# Multi-account client and source registration gaps

Recorded 2026-11-18 from operator feedback. This is a replacement work order for the current
interactive source-agent UX. It supersedes the one-state-directory/one-source model and the
requirement for a human to supply a dataset identifier or origin proof public key. It does not
claim that the following behavior is implemented.

Read BOOTSTRAP-GAPS.md and REVIEW-2026-11-19.md for earlier defects. Once their live password
login and pending source retry proof is accepted, preserve it. Do not reopen a rejected public
dev ingress or the password login mechanism merely to redesign client-side account management.
Do not reintroduce reverse-h2; source dispatch remains grpctunnel-only.

## Product contract

The executable follows the shape of the MinIO `mc` client. A single installed binary has a
persistent local account store and can run registrations/workers for several accounts and
sources at once. The account store belongs to the agent process and the sources are associated
with its accounts. It is not a global gateway singleton or four manually supplied state folders.

### N1. Account login or connection, not source enrollment (HIGH)

Required command family, with `connect`/`login` support and conventional aliases as appropriate:

- `source-link account add <account-alias>` or `source-link connect <account-alias>`
- `source-link account list`
- `source-link account select <account-alias>` or the documented equivalent
- optional noninteractive scripted equivalents, explicitly not required for normal use

Every first-time, second-time and later account addition asks only:

- gateway URL
- username
- password, with terminal echo disabled

It asks nothing else. Authenticate using the working gateway password-exchange contract, obtain
only that ordinary user's scoped credential, and persist it owner-only. Never print or persist
the raw password. The gateway never authorizes source content merely because this login succeeds.

The local account alias is chosen by the user and distinguishes one account/gateway pair from
another. It is not an MCP source name and does not require an existing LiteLLM key to reach the
normal login exchange. Repeating the same account addition must not silently replace a live
credential or confuse source ownership; return the existing account or require an explicit,
confirmed credential rotation command.

`account list` contains aliases, usernames/gateway URLs and state suitable for local operation,
never password/key/HMAC material. Each selected account locally persists its scoped user
credential at mode 0600, with permission checks and atomic writes. Preserve the documented
password-key exchange that was proven live; no manual `.env` or `user.key` editing is needed.

### N2. Persistent multi-source registry and concurrent workers (HIGH)

The current setup.PathsFor layout is inadequate: one `user.key`, `bootstrap.json` and
`identity.json` can represent only a single combined user/source registration. Creating four
separate state dirs is not a supported user experience and loses the requirement of several
accounts simultaneously active in one agent.

Replace it with a local state model expressing these relations:

- account: account alias, gateway URL, username, separately secured scoped user credential
- source: auto-derived source name, selected owning account, target, persisted worker identity
- stable local source identifier used by service configuration and lifecycle operations

Multiple sources and multiple accounts may be active at the same time. `connector` without
arguments runs all enabled local sources in one process/persistent worker pool or runs the
selected, explicitly named source if that is intentionally supported. Document which. A systemd
service may have one stable ExecStart; registering a new source may require an explicit restart
command, not prompt for another gateway login.

Normal source registration (`source source add`, `source register` or one well-documented
equivalent) must not ask again for gateway URL/username/password. It must read the selected
account or an explicit `--account`. The OpenAPI document is obtained from the registered origin
or supplied as a validated documented input without leaking credentials or catalog contents.
Do not force the user to repeat six arbitrary fields that have derivable answers.

Existing sources are discoverable through nonsecret account/source listing. Revocation and
identity replacement must select the source rather than rely on the one old global identity file.
Generation fencing, owner authorization, credential separation and approval isolation are
preserved. Tests must use separate sources on disk, not one persistent in-memory registry that
masks overwrites.

### N3. Auto-derived, collision-safe source names (HIGH)

The human does not need to choose six identifiers for a simple source. Derive a stable source
name deterministically from canonical identity: the gateway-derived owner plus the target's
normalized host/IP and port, within the selected gateway/account scope. A human-friendly optional
source description is fine only if it does not create routing collisions.

Account-scoped owner identity is required: identical source target tuples registered by two
different ordinary users are different sources, not a global duplicate. The same normalized
tuple for the same authenticated owner is the same source regardless of accidental second CLI
invocation. Do not randomly generate a new alias every time it is typed.

A successful repeated registration returns a specific `MCP source already exists` response
with its existing stable name/ID and does not create a new MCP row, artifact, pending approval,
worker generation or enrollment identity. If content or target policy changed, existing tokens
and evidence cannot silently authorize an overwrite; the returned message must name an explicit
new-source/change/review path. Admin approval is not an idempotency key.

The UI/catalog should not gain names that overlap by confusing local aliases, gateway source
IDs and worker IDs. Enforce this server-side within its transaction; client-side display-name
checking alone cannot prevent simultaneous writers.

### N4. Remove the circular human origin-proof requirement (HIGH)

Current behavior cannot be shipped as a usable self-service contract: `RemotePeer.proof_key` is
mandatory, `collect()` asks for an opaque base64 Ed25519 public key, and no canonical provision
of the corresponding private key exists for the service. An ordinary operator is not expected
to understand, generate and install this pairing.

The current end-to-end proof is not an independent proof of origin. The broker obtains
`/openapi.json` through the connector's own target-selected tunnel, while the connector may own
or arrange both the target response and the signature/key it supplies. `catalog.Verify` checks
a signature over nonce/source/dataset/catalog; it does not establish which program handled the
HTTP request or that the authenticated user's origin owns the network target. Generating both
halves in the connector, or asking a user to copy the public half to a separate service, adds
ceremony without changing that trust property. Option B in the worker's proposal is the honest
choice for this local/OpenAPI flow, subject to the target/admission safeguards below.

Remove the human dataset-ID and proof-public-key prompts, the signature request/response
requirement and its pseudo-origin-attestation from this supported product path. If dataset_id
is actually catalog fingerprint state, derive it from the canonical OpenAPI bytes server-side;
do not invent an identity based on the Unix username. If no additional protocol uses it, remove
the redundant peer field/schema requirement deliberately. Do not backfill Prisma or delete old
state during boot.

The allowed-source content is still checked for validity and canonical digest, the gateway
still owns approval, and the connector still authenticates its per-source generation-scoped
tunnel identity. Nothing here claims a gateway username/password validates the remote target's
network ownership. Where independent third-party-origin proof is needed in future, design a
real, optional integration with that origin's public key and signature gathering. It must not
be a required opaque public-key prompt or a claim that an agent proves what it controls.

### N5. Targets can be IP addresses or Kubernetes service names (HIGH)

Literal `Target(host: Literal["127.0.0.1"])` and loopback-only Go validation cannot represent
an origin in another Pod/namespace or on a private host. `originTransport` and gRPC worker
forwarding resolve the stored target locally from different brokers/nodes; HTTP may send an
arbitrary Host header that differs from the requested origin URL. Do not confuse IP literal
validation with actual reachability or eliminate Host-semantics checks mechanically.

Accept IPv4/IPv6 addresses and valid single Kubernetes service DNS names, including port.
For the K8s-focused initial release support `svc.namespace.svc` or the equivalent full,
normalized cluster-service name, not an arbitrary URL scheme, userinfo, shell expression or
DNS rebinding sequence. Reject public/external destinations outside an explicit approved policy
unless the deployment contract explicitly permits them. Preserve actual host/port/Host-header
rules across the gateway's request validation, forwarded source identity and origin transport.

This changes security scope. Self-service authenticated owner plus existing approval is the
admission boundary for exposing private non-loopback network destinations; ordinary enrollment
never makes it visible before approval. Document that this is an exposure authorization, not
remote cryptographic origin attestation. Include source target changes under immutable
same-source registration policy or a separately reviewed change workflow.

### N6. Integration tests and dev proof (HIGH)

Execute real binary commands on a fresh isolated HOME:

- account A and B login: only URL, username and password each time
- same account A source addition: no login again
- account A second source and account B source: both stay active; inspect persisted state/files
- repeat exact source registration: explicit already-exists outcome, DB/registry unchanged
- two owners with an equal host:port do not collide
- IP and K8s-service-name registrations are admitted; malformed/public/unapproved targets fail
- no dataset/proof-key input or secret output
- restart/reload resumes each configured identity and generation
- revocation marks only the selected source terminal and replacement is supported
- admin approval remains required; an MPA/worker registration is not an approved tool dispatch

Then execute a live isolated dev flow with a real ordinary account, with admin approval and a
real K8s Service origin, through the dev kubectl port-forward for REST. Do not create a new dev
public ingress, mutate live production DNS/nginx, expose new public tunnel routes, or use real
user secrets. Public hostname tunnel dev exposure removal is a separate deployment correction;
lack of such an ingress does not cancel the need to prove this flow. Document any tunnel
connectivity prerequisite separately rather than quietly re-enabling the prohibited route.

Existing installer rendering, persisted gateway, transient retries, pending-source conflict
fixes and prior tunnel compatibility tests should be preserved. Update the implementation doc
and installer from the actual eventual CLI contract; remove old six-prompt instructions and
one-source-directory recipes. Fresh operator examples must not ask a human for proof key/json,
dataset ID, gateway environment files or repeated login for each source.

Acceptance requires targeted regression tests failing old behavior, Go/race/sidecar compatibility,
focused Python checks, static analysis, a real executable proof and cleanup. Do not answer with
four manual state dirs, only a new proposal for self-signed connector credentials, or a choice
between two public-dev networking architectures the operator did not request.
