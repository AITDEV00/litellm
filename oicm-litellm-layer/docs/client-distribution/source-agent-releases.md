# Source-agent artifacts: adapt the `/install` distribution pattern

Status reviewed 2026-11-26: partially implemented review, not a wholesale unimplemented design. The generic `/install` channel, immutable source-agent release paths and conditional active-pointer compare-and-set have since landed. Immutable-version checks, valid release pair activation and concurrent pointer protection are current source contracts. A source-agent installer is documented and live on prod; use the current implementation as the behavioral reference, not every claim below as an unsatisfied blocker

Still unproven or required: live RustFS publication through the operator target; full publication to real install validation; clean-HOME digest and lifecycle proof; explicit convention for generated install-template versus artifact bytes; revision fences during different-byte racing uploads; and the separately stated connector-init/runtime gaps. This documentation stays as a requirements/history review of remaining safeguards. For the installer implementation, see [Client installation](../mcp/client-install.md); for current publisher status, see [running broker status](../mcp/source-broker/running-system/README.md#status-register)

This page follows the existing cc-switch `/install` host model and proposes safeguards for source-link. It must not discard that transport by proposing a new client-release architecture, and should not be interpreted as a claim that all of these requirements remain unimplemented

Read first: [architecture](architecture.md), [admin-sync](admin-sync.md), [rustfs](rustfs.md),
[security](security.md), and the detailed independent review at
`../../mcp-source-link/REVIEW-2026-11-19.md`

A repository verification on 2026-11-12 already proved the desired raw template operation on
dev: `templates/install.sh.tmpl` was uploaded via the gateway as application/octet-stream,
then raw stored bytes matched the local template and `/install` rendered its anticipated content
without updating the server image. It also verified that the inventory ETag need not be plain
SHA-256, so the upload response digest and the real byte readback are the meaningful checks.
If your reference for reuse was described as already implemented, this is the exact precedent

## The already-existing artifact-channel answer

Yes, admin artifacts are uploaded **through the gateway into RustFS**. The workflow is not
"open an unauthenticated/mc write route to RustFS", and not "manually pack each artifact into
base64 JSON".

The established cc-switch design is:

```
local file
  -> PUT /install/admin/v1/artifact/{path}
       Authorization: Bearer <per-environment Proxy Admin key>
       Content-Type: application/octet-stream
       --data-binary @local-file
  -> gateway-only RustFS credentials
  -> selected environment's client-distribution bucket
  -> admin publishes the active manifest/pointer last
  -> unauthenticated public artifact download through the same gateway
```

The administrator needs only the per-environment Proxy Admin credential for the gateway. They
never receive or supply the store's own credentials and never use a raw S3/mc endpoint. The
slice owns the gateway-to-RustFS boundary, credentials and all object-store writes. RustFS has
no public ingress. Gateway and store scheduled service credentials stay off source/user hosts

Source-agent must use this **same-origin raw artifact upload channel**, with path containment
under `source-agent/` and content size/sha256/inventory semantics reconciled with the generic
channel. A small operator command wrapping these calls (plus manifest generation) makes the
operator experience concrete; it must actually call the raw endpoint and validate all outputs.

This channel *does not* discard immutable released versions. New versions get new compatible
`source-agent/<version>/source-link` paths; merely overwriting that binary path with different
bytes is not an acceptable release workflow. The existing general artifact endpoint supports
repeated uploads, so the write handler itself must reject differing contents for immutable
release paths (see current discrepancies below), or reuse/extend that handler under an explicit
immutable-only release subtype without making every release-type data replacement impossible.

## What to copy from cc-switch (exact implementation pattern)

### Per-environment store

A prod bucket (`litellm-client-mirror`) and a dev bucket (`litellm-client-mirror-dev`) already
exist with creds held by the respective gateway, whose S3 client streams uploaded/read bytes.
`GATEWAY=prod|dev` or an explicit URL selects the endpoint/environment, not guessable bucket
credentials. There is no Kubernetes Image deployment implicit in uploads.

### Reserved layout (versioned artifacts plus one stable template)

```
source-agent/<version>/source-link            immutable binary bytes
source-agent/<version>/release.json           version/checksum/architecture/compat metadata
templates/source-agent-install.sh.tmpl         operator-owned installer logic, not version-bound
source-agent/manifest.json                     publication pointer/version policy (recommended)
```

A standalone script template stays valid across binary versions. Release metadata selects the
current client version. This matches cc-switch's `templates/install.sh.tmpl`, which is fetched at
request time and only then rendered using a store manifest and the gateway's canonical base URL.
A template is not the artifact you upload unchanged to the user's exec path full of placeholders.

**Required installer behavior:**

- `GET /install/source-agent` loads the current source-agent manifest and validates the
  per-version binary and its manifest sizes/hashes before producing a script.
- Load `templates/source-agent-install.sh.tmpl` from RustFS exactly like the general installer
  does `templates/install.sh.tmpl`. An intentionally minimal baked-in recoverable default is
  optional for missing template only, not a stale duplicate script logic used as silent
  fallback after an upload. Never hide a new binary release behind an old-image script.
- Render at request time with a typed, trusted base URL (same `_get_base_url` pattern), version,
  sha256 and **no credential**. Do not return `templates/source-agent-install.sh.tmpl` or the
  checked-in `install.sh` as a still-placeholder operational script.
- A safe alternative is a template that contains only the canonical base URL and resolves
  version/hash from the published release manifest on the source host. It still **renders a
  validated value** and downloads a real release selected by the gateway, rather than relying
  on the broken upload-time substitution nobody implements.
- `source-link` and `release.json` are streamed from RustFS via versioned public read URLs.
  The installer verifies `sha256(source-link)` before chmod/exec, invokes `source-link init`,
  and only starts the service after valid initialization.
- No reserved `__STATE__` manually interpolated elsewhere. Resolve the source host state dir
  in the shell from the same validated `HOME`/SOURCE_AGENT_STATE that is assigned in the script;
  the generated systemd unit has no secret-bearing env or stray literal placeholder.

An installer change should be an admin upload of `templates/source-agent-install.sh.tmpl` under
its own permitted path. No changed binary or Docker build is necessary. A template-only edit
needs no manifest version bump: the next render reads that new template. A new binary version
requires manifest activation before use. When script + binary are deliberately tested together,
publish both before advancing `source-agent/manifest.json`; then prove the actual installer
chooses the new binary with that template. Users need not know any of these storage keys.

### Publish, rollback, verify

Add an equivalent distinct source-agent operator path wrapping the existing raw API:

- default fails when environment/target or `VERSION` absent; never infer prod from omission
- `--directory` with `source-link` and `release.json` metadata, or a Make target deriving
  those artifacts with explicit parameters
- SHA-256 statically-built executable validation, templated syntax validation (`sh -n`),
  required fields/expected OS/arch and compatibility metadata checked
- upload the manifest-referenced new artifacts first using raw bytes + computed digests
- as with cc-switch's `sync-mirror.sh`, administrator credential only in Authorization header/
  curl env, never URL argv, logs, base64 scripts, JSON wrappers or commit git secrets
- active pointer published last, validates all referenced immutable bytes/hashes, no half-pair
- resumed uploads no-op only if same bytes at same version; disagreement = conflict, never
  silently replace a different in-use executable. Template changes are separate releases;
  keep audit in local git history. `source-agent/<version>` tuple strictness enforced server
  side (not just in the operator's command)
- same checked conditional handoff/serialized update for active manifest, with two concurrent
  writers failing/safely retrying instead of last-write-wins dropping each other's release
- rollback is another atomic pointer operation selecting a prior complete immutable pair,
  with binary/template compatibility verified. No editing old binary bytes

Example operator target, not evidence of a working implementation:

```
make mcp-source-agent-publish GATEWAY=dev VERSION=0.5.0
```

That command uses a read-to-repack directory or derives version metadata, issues the raw
artifact PUTs through the dev gateway, advances the checked pointer and prints hashes only.
It may build the Go binary but must not build container images, roll the gateway or delete the
broker's sidecar. The currently claimed `mcp-sidecar-publish` is *not* accepted until it performs
this behavior and its real resulting download passes installation.

Observe before/after gateway pod image+creation time, upload through dev HTTP port-forward,
verify the installer and new binary hashes from a clean HOME. The outside source connector's
HTTPS requirement is an independent topology item (R9), not permission to pretend plain HTTP
port-forward input works transparently as HTTPS in the CLI.

## Previously reviewed discrepancies, now superseded in implementation

The older text below described generic upsert, no pointer CAS, and a base64 publisher. Current implementation resolves those exact issues. `routes.py` rejects differing bytes at immutable source-agent release paths. `source_agent_distribution.py` performs separate activation with a valid version pair, `expected_current`, `IfMatch` / `IfNoneMatch`, and reads the RustFS installer template at request time

The source-agent generic mirror manifest schema is distinct from the cc-switch schema-5 manifest, and release uploads use the shared raw artifact endpoint and their own source-agent activation endpoint. No live end-to-end operator publication is claimed by this correction. The notes below stay for design provenance, not as a current API source map

## Historical discrepancies between the older design docs and current generic routes (2026-11-18 snapshot)

- `PUT /install/admin/v1/artifact/{path}` computes sha256 from the request, returns an
  `unchanged` flag for exact repeated bytes, but otherwise permits a write for the same key.
  It is an **upsert**, not an enforced general immutable-object write.
- `admin_put_manifest` validates selected references but does **not** accept/compare HTTP
  `If-Match` or otherwise guard the put against a concurrently changed manifest ETag. The
  admin-sync text describing ETag compare-and-swap (and releases with "no lost update") is
  therefore only an intended invariant at present, not proven implementation.
- The general mirror's manifest schema 5 is a different DTO for cc-switch/harness artifacts;
  do not feed `source-agent/manifest.json` to that URI without a dedicated source-agent
  release schema/validator. Reuse storage raw-artifact routing and viewflow, not mutually
  incompatible shape. `head_object` only proves that named object exists in generic reference
  validation; source-agent must validate that requested byte hashes agree first.
- Current source-agent uploader is **not aligned with this pattern**. It is one JSON body with
  base64 binary + release JSON + optional installer, copied verbatim into versioned objects.
  It also changes the *active pointer* to older assets with no client installer and returns 409
  for future fresh installs. Do not carry this forward as an alternative accepted pathway.
- `_validated_install_script` looking for the text " init" guards neither executable script
  correctness nor placeholder completion nor its service-start contract. It cannot substitute
  for testing the real uploaded installer and actual systemd service startup.

Required work: implement the claimed inviolable hash-no-overwrite + checked-pointer semantics
explicitly in the artifact/release slice (both slices if they share auth/state decisions), add
true concurrency tests to prevent hidden update clobbering, then correct the legacy docs to state
what is actually implemented before claiming the pattern has settled.

Shared facility *mechanism* for the new slice:

```
admin raw artifacts in -> validator/checksum/path scope -> RustFS current bytes
           public manifest/version selection -> valid gateway-rendered installer
                                   -> actual static binary hash verified -> source-link init
```

No third RustFS access pattern, no new client-side signing protocol, and no new shared-store
credential for the admin's machine. Client releases change no running server code path.

## What remains separate from this distribution fix

The new guided `source-link init` channel/auth constraints and R1-R9 are still independent
blocked implementation work. Giving the installer a correct RustFS home does not fix login,
persistent runtime URL after init, durable two-key background behavior, approval-to-catalog
inconsistency, lost-worker response semantics, revocation workflow, or dev tunnel topology.

The final result must be exercised by using the real publisher -> real served installer ->
real `init` -> zero-restart resumed service, not unit-test fixtures which substitute values in
shell before checking a component that never performs that substitution.

## Change log

- 2026-11-18: Independently recorded after worker additions `ebef0d6f75f..53ef2522d24`.
  Verified the expected raw gateway artifact + stored-template pattern in docs and code. Also
  captured along this review the upsert/CAS docs-vs-code mismatch so no implementation agent
  assumes already passing invariants that the implementation does not exercise.
