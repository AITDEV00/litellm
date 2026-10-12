# Admin Sync API

The PVC can be deleted accidentally, and it can only be filled by someone with
internet access. This doc defines the REST surface the admin uses to push,
upsert, or delete mirror artifacts idempotently, over the gateway, with no
`kubectl cp` and no cluster access.

## Why on the gateway

The slice already runs inside the app, so the admin API is a second
`APIRouter` in the same module, mounted the same way. It reuses the exact
admin-auth pattern OICM already uses: `dependencies=[Depends(user_api_key_auth)]`
plus a `_require_admin` check against `LitellmUserRoles.PROXY_ADMIN` — see
`litellm/proxy/oicm_routes.py` (`_require_admin`) for the precedent. The
gateway's master key authenticates as a proxy admin, so the admin's existing
bearer token works with no new credential type.

No new auth surface: the master key is already in the `litellm-master-key`
Secret and already grants admin, so this adds routes, not a key.

## Routes

All under `/client-mirror/v1/*`, admin-only. Deliberately distinct from the
public read paths (`/mirror/*`, `/install`) so read and write can never be
confused and so the write surface can be firewalled separately if wanted.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/client-mirror/v1/manifest` | Read the current manifest (admin view, includes hashes) |
| `PUT` | `/client-mirror/v1/artifact/{path}` | Upsert one artifact; body = raw bytes; idempotent |
| `PUT` | `/client-mirror/v1/manifest` | Upsert the manifest atomically; idempotent |
| `DELETE` | `/client-mirror/v1/artifact/{path}` | Delete one artifact; idempotent (404-free) |
| `GET` | `/client-mirror/v1/inventory` | List stored artifacts + sizes + hashes |

`PUT` semantics are upsert: writing the same bytes twice is a no-op and
succeeds. The server computes the sha256 on write and compares; if the stored
object already matches, it returns `200` with `"unchanged": true` and writes
nothing. That is what makes re-running the sync idempotent.

### Request/response shape

`PUT /client-mirror/v1/artifact/{path}` takes the raw bytes as the body
(`Content-Type: application/octet-stream`), not multipart, so a plain
`curl --data-binary @file` works and the server streams to disk without
buffering the whole artifact in memory (the Python artifacts are tens of MB).

```json
// 200 response
{"path": "uv/0.10.9/uv-x86_64-unknown-linux-gnu.tar.gz", "sha256": "...", "size": 20341000, "unchanged": false}
```

The manifest `PUT` validates the JSON against the schema in
[mirror-content.md](mirror-content.md) and, critically, verifies that every
artifact it references exists on the volume with a matching hash. A manifest
that points at a missing or mismatched artifact is rejected `409`. This keeps
the invariant that the manifest never lies about what is stored.

## Idempotent sync flow for the admin

```sh
KEY="$LITELLM_MASTER_KEY"                     # from the Secret, never in a file
BASE=https://litellm.ecouncil.ae

# 1. push every artifact (upsert; unchanged ones cost one hash, no write)
for f in $(find ./mirror -type f ! -name manifest.json); do
  rel="${f#./mirror/}"
  curl -fsS -X PUT "$BASE/client-mirror/v1/artifact/$rel" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$f"
done

# 2. publish the manifest last, atomically
curl -fsS -X PUT "$BASE/client-mirror/v1/manifest" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  --data-binary @./mirror/manifest.json

# 3. verify
curl -fsS "$BASE/client-mirror/v1/inventory" -H "Authorization: Bearer $KEY"
```

Properties:

- **Order-independent for artifacts, manifest last.** Artifacts can be pushed
  in any order and re-pushed freely; only the manifest publish is the
  commit-point, and it refuses to publish unless everything it references is
  present and hashes match.
- **Resumable.** A dropped connection mid-upload leaves the store consistent
  (either the object landed fully or not at all — the server writes to a temp
  file and renames); re-running the loop re-uploads only what changed.
- **Deletion is explicit.** Removing an artifact is a `DELETE`; the manifest
  must be re-published afterward or the delete is rejected while the manifest
  still references it. Prevents a half-deleted store from being advertised.
- **No partial state visible to users.** Public `/mirror/*` reads go through
  `StaticFiles` on the same volume; because writes are temp-file + rename, a
  user never sees a half-written artifact.

## Concurrency

Two admins syncing at once is safe for artifacts (last write wins, same bytes
either way) and safe for the manifest (the `PUT` is a single atomic rename).
The only interleaving worth guarding is "A publishes a manifest, B deletes an
artifact A's manifest references" — the delete route re-checks the current
manifest and rejects the delete with `409` if it is still referenced. Resolve
by publishing a manifest that drops the artifact, then deleting.

## What this replaces

The earlier draft had the admin use `kubectl cp` or rsync-over-port-forward.
That needed cluster access and had no idempotency. The API needs only the
master key the admin already has and is idempotent by construction, which is
what a recoverable-from-accidental-delete workflow requires.

## Security notes

- Admin-only via `_require_admin` (the `oicm_routes.py` precedent), which
  rejects anything that is not `PROXY_ADMIN` with `403`.
- Path parameters are validated: no `..`, no absolute paths, no symlink
  escapes. The handler resolves the path against the mirror root and rejects
  anything that escapes it (`os.path.realpath` containment check), so the
  write API cannot be used to write outside the mirror directory.
- The write surface is separate from the public read surface, so an
  `ingress` rule can restrict `/client-mirror/*` to an admin network range
  without touching the public `/mirror/*` reads.
- Size limits: a per-request body cap (reject `413` above it) so a runaway
  client cannot fill the PVC in one request; the expected largest artifact is
  tens of MB, so the cap sits comfortably above that.
