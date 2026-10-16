# Admin Sync API

The admin must be able to fill and repair the artifact store from a machine
with internet access. This doc defines the REST surface for that: push, upsert,
or delete artifacts idempotently, over the gateway, with no `kubectl`, no
`mc`, and no direct object-storage access.

## Why the API, not direct RustFS access (Conflict A resolution)

The alternative was to have the admin point `mc` / the S3 API straight at
RustFS. Rejected: that desyncs the manifest from the objects, because the
manifest's shape, the hash-on-write check, and the "publish only if every
referenced object exists" invariant are logic that RustFS does not provide.
Two writers (the admin's `mc` and any future tooling) would each have to
reimplement that logic, and would drift.

So the admin talks to the slice, and the slice talks to RustFS. One place
enforces the invariants. The gateway is the only thing with RustFS credentials;
the admin only ever holds the LiteLLM master key.

## Why on the gateway

The slice already runs inside the app, so the admin API is a second router in
the same module. It reuses the admin-auth pattern OICM already uses:
`dependencies=[Depends(user_api_key_auth)]` plus a `_require_admin` check
against `LitellmUserRoles.PROXY_ADMIN` — see `litellm/proxy/oicm_routes.py`
(`_require_admin`) for the precedent. The gateway's master key authenticates
as a proxy admin, so the admin's existing bearer token works with no new
credential type.

## Routes

All under `/install/admin/v1/*`, admin-only. Nested under `/install` per
decision 1, but kept on its own sub-path so the ingress can restrict writes by
network range without touching public reads.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/install/admin/v1/manifest` | Read the current manifest (admin view, includes hashes) |
| `PUT` | `/install/admin/v1/artifact/{path}` | Upsert one artifact; body = raw bytes; idempotent |
| `PUT` | `/install/admin/v1/manifest` | Upsert the manifest atomically; idempotent |
| `DELETE` | `/install/admin/v1/artifact/{path}` | Delete one artifact; idempotent |
| `GET` | `/install/admin/v1/inventory` | List stored objects + sizes + hashes |

`PUT` semantics are upsert: writing the same bytes twice is a no-op and
succeeds. The slice computes the sha256 on write (streaming into RustFS) and
compares; if the stored object already matches, it returns `200` with
`"unchanged": true` and writes nothing. That is what makes re-running the sync
idempotent.

### Request/response shape

`PUT /install/admin/v1/artifact/{path}` takes the raw bytes as the body
(`Content-Type: application/octet-stream`), not multipart, so a plain
`curl --data-binary @file` works and the slice streams to RustFS without
buffering the whole artifact in memory (the Python artifacts are tens of MB).

**Always send the explicit header.** curl defaults a `--data-binary` body to
`Content-Type: application/x-www-form-urlencoded`, which sends auth down
Starlette's form parser (`request.form()`); the form parser consumes the ASGI
stream, and the handler's `request.body()` then fails with `RuntimeError:
Stream consumed` (an opaque 500). With `application/octet-stream`, auth
pre-reads and caches the body and the handler reads it from cache.

```json
// 200 response
{"path": "cc-switch/5.11.1/cc-switch-cli-linux-x64-musl.tar.gz", "sha256": "...", "size": 8960000, "unchanged": false}
```

The manifest `PUT` validates the JSON against the schema in
[mirror-content.md](mirror-content.md) and verifies every referenced object
exists in the bucket with a matching hash. A manifest that points at a missing
or mismatched object is rejected `409`. This keeps the invariant that the
manifest never lies about the store.

## Immutability + manifest compare-and-swap

- **Immutable artifacts.** A `cc-switch/<version>/<file>` object, once
  published with a given sha256, is never overwritten with different bytes.
  The slice enforces this: a `PUT` whose bytes hash differently from the
  existing object at that path is rejected `409`. A new build gets a new
  version path, so "same path, different content" cannot happen.
- **Manifest compare-and-swap.** The manifest `PUT` takes the current
  manifest's ETag as an `If-Match` precondition. Two concurrent admin syncs
  cannot silently clobber each other: the second `PUT` fails `412` and must
  re-read, re-merge, and retry. This replaces "last write wins" for the
  manifest, which could drop a concurrently-published artifact reference.

## Idempotent sync flow for the admin

```sh
KEY="$LITELLM_MASTER_KEY"                     # from the Secret, never in a file
BASE=https://litellm.ecouncil.ae

# 1. push every artifact (upsert; unchanged ones cost one hash, no write)
for f in $(find ./mirror -type f ! -name manifest.json); do
  rel="${f#./mirror/}"
  curl -fsS -X PUT "$BASE/install/admin/v1/artifact/$rel" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$f"
done

# 2. publish the manifest last, atomically
curl -fsS -X PUT "$BASE/install/admin/v1/manifest" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  --data-binary @./mirror/manifest.json

# 3. verify
curl -fsS "$BASE/install/admin/v1/inventory" -H "Authorization: Bearer $KEY"
```

Properties:

- **Order-independent for artifacts, manifest last.** Artifacts can be pushed
  in any order and re-pushed freely; only the manifest publish is the
  commit-point, and it refuses to publish unless everything it references is
  present and hashes match.
- **Resumable.** A dropped connection mid-upload leaves the store consistent
  (the object either landed fully or not at all); re-running the loop
  re-uploads only what changed.
- **Deletion is explicit.** Removing an object is a `DELETE`; the manifest
  must be re-published afterward or the delete is rejected while the manifest
  still references it.
- **No partial state visible to users.** Public reads proxy from the same
  bucket; because writes are atomic per object, a user never sees a
  half-written artifact.

## Concurrency

Artifacts are immutable and content-addressed by version+path, so two admins
pushing the same version push the same bytes — no conflict. The manifest is
guarded by the compare-and-swap precondition above, so a concurrent publish
cannot silently drop another sync's references. The one interleaving still
worth guarding is "A publishes a manifest, B deletes an object A's manifest
references" — the delete route re-checks the current manifest and rejects the
delete with `409` if it is still referenced. Resolve by publishing a manifest
that drops the object, then deleting.

## Security notes

- Admin-only via `_require_admin` (the `oicm_routes.py` precedent), which
  rejects anything that is not `PROXY_ADMIN` with `403`.
- Path parameters are validated: no `..`, no absolute paths, no escaping the
  mirror prefix. The handler normalizes the key against the bucket prefix and
  rejects anything that escapes it, so the write API cannot touch other
  buckets (notably not `litellm-logs`).
- The write surface is separate from the public read surface
  (`/install/admin/*` vs the rest of `/install/*`), so an ingress rule can
  restrict it to an admin network range.
- Size limits: a per-request body cap (reject `413` above it) so a runaway
  client cannot fill the bucket in one request.
