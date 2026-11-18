# Connector-owned enrollment, recorded 2026-11-18

Status: OPEN REQUIREMENTS. These are blockers for developer acceptance testing of the broker.
Nothing in this document describes shipped behaviour. Two required behaviours are absent from
the tree, and today's substitutes are wrong in ways this file records.

## Why this file exists

A walkthrough of the broker was started on 2026-11-18 against the dev cluster, intending to
demonstrate a source host exposing a private service to the gateway. The walkthrough stalled
because the flow it needed does not exist. Two requirements surfaced that the shipped
implementation does not meet, and both were mis-stated in earlier documentation, including
`running-system/README.md` and `byom-self-service.md`. This file is the owning record for the
correction. Where it disagrees with those files, this file is the requirement and the code is
the current, deficient reality.

## Requirement 1: the identity is a user, not a team-scoped virtual key

A source is exposed by a **person** acting as a gateway user. The connector must authenticate
as that user's own account identity, not as a virtual key scoped to a team.

What the code does today:

- `litellm/proxy/client_distribution/source_agent_distribution.py` tells the operator to place
  "a team-scoped virtual key" in `connector.env`. That wording is wrong.
- `gateway.py::enroll` rejects a caller with no `team_id`
  (`raise HTTPException(400, "enrollment requires a team-scoped key")`), so a purely
  user-scoped key cannot enroll. The owner is already derived from `user.user_id`, which is the
  correct half; the `team_id` gate is the defect.

What is required:

- The connector authenticates as an ordinary gateway **user account**, and the source owner is
  that user. No team scoping is a precondition for enrolling.
- Documentation, installer text and error messages must say "user", not "team-scoped virtual
  key". A reader must not be told to create a team-scoped key.

Open question this raises, to resolve before implementation: where does that user's credential
come from, given the connector does not yet exist on the host when the flow starts. See
Requirement 2, which is the same problem viewed from the other end.

## Requirement 2: the connector owns the gateway paths, not the human

The connector binary is the only supported client of the broker's registration surface. Every
path that registers, enrolls, or wires a source belongs to the binary. A human invoking those
paths directly is the admin case only.

What the code does today:

- `mcp-source-link/main.go` accepts exactly two arguments, `worker` and `connector`
  (`usage: mcp-source-link worker|connector`). There is no register, enroll, or attach
  subcommand.
- The connector dials the tunnel and nothing else. It makes no call to any gateway REST route;
  its only outbound requests are to the private origin (`internal/connector/tunnel.go`).
- Consequently every step that today precedes a working tunnel is performed by hand:
  `POST /mcp-sources` to enroll, `POST /mcp-source-link-sources/{id}/workers` to register a
  worker, and then deriving `MCP_SOURCE_LINK_CONNECTOR_CONFIG` by computing
  `HMAC(pepper, "remotedialer-credential", enrollment_id, worker_id, generation)` from the
  broker-side pepper. That derivation is impossible for anyone without the pepper, so in
  practice the identity is minted by an operator with cluster access.

What is required:

- The binary performs the registration and enrollment calls itself, on behalf of the user it
  was configured with. A user on a source host runs the connector, and the connector talks to
  the gateway; the user does not curl gateway endpoints.
- The binary obtains its own connector identity. A human must never need the broker pepper, and
  must never hand-assemble `connector.env` or the connector identity file.
- The gateway registration routes stop being a human-facing surface. Direct invocation is an
  admin action, and should be authorised as one rather than being reachable by any user with a
  key.

Explicitly not a requirement: adding a new bespoke registration protocol. The existing gateway
routes are the right mechanism. What changes is who calls them.

## What this supersedes

These statements, written earlier the same day, are wrong and must be corrected where they
appear as current behaviour:

- `byom-self-service.md`: "the connector authenticates enrollment with a team-scoped virtual
  key" and the claim that the bootstrap question is closed by that. It is not closed; it is
  Requirement 1 and Requirement 2.
- `running-system/README.md`: the Status register row for bootstrap credential exchange marked
  DONE, and the paragraph describing the team-scoped key as the answer.
- `source_agent_distribution.py` module docstring, which states the team-scoped key shape.

The flow sketched in `byom-self-service.md` (step 2, "connector calls `POST /mcp-sources` with
`Authorization: Bearer <team-key>`") was directionally right about the connector making the
call and wrong about the credential being team-scoped. Requirement 2 keeps the call site and
changes the credential.

## What remains true and is not in scope here

The tunnel, the approval gate, the catalog-fingerprint probe, generation fencing, revocation,
and header stripping are implemented and were verified live. The pending-review gate that keeps
an unapproved source from serving is correct. The transport work is not being reopened.

The two FastAPI services and two MCP servers stood up on the source host for the acceptance run
are fixtures, not part of the product. Only the OpenAPI-shaped services can be carried by the
broker, because `canonical_spec` requires an `openapi: 3.x` document with `paths`; a native MCP
server has no such document and is rejected. That asymmetry is expected and unrelated to these
requirements.

## Acceptance for these requirements

A user with an ordinary account, on a host that the cluster cannot reach, installs the
connector and runs it. Without an administrator touching the cluster, and without the user
calling any gateway route by hand, the source appears for review owned by that user, opens its
tunnel, and after approval serves a real read. The user never sees the pepper, never writes
`connector.env` by hand, and never derives a credential.

Until that is demonstrated, developer acceptance testing of this broker is blocked.
