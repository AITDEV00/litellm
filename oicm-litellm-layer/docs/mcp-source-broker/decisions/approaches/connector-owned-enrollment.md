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

## Who the accounts are, and where they come from

Gateway user accounts are created when the gateway is deployed, from the user and key entries in
the kube manifests for the environment. That is the deployment phase's job and it already happens
there, so nothing in this requirement asks anyone to create accounts as part of a source flow.

What the requirement does fix is whose account a source belongs to. Each person who exposes a
source uses their own deployed gateway user account, and that account's own virtual key is the
credential the connector is configured with. Every gateway call the connector makes carries that
key, so the gateway sees the calls arriving as that user and the source owner is that user.

The admin and master key belong to the gateway administrator for administration. Neither is
handed to a source host, and the connector must never hold, receive, or be configured with them.

## Requirement 1: the identity is the user account, not a team-scoped key

A source is exposed by a **person** acting as a gateway user. The connector authenticates with
that person's own account key. Team membership is incidental and is never a precondition for
exposing a source.

What the code does today:

- `litellm/proxy/client_distribution/source_agent_distribution.py` tells the operator to place
  "a team-scoped virtual key" in `connector.env`. That wording is wrong.
- `gateway.py::enroll` rejects a caller with no `team_id`
  (`raise HTTPException(400, "enrollment requires a team-scoped key")`), so a user-scoped key
  belonging to no team cannot enroll at all. Deployed user accounts are not team members, which
  makes this the exact case that breaks. The owner is already
  derived from `user.user_id`, which is the correct half; the `team_id` gate is the defect.
- `route_checks.py` comments its `/mcp-sources` exemption with "the team-scope requirement is
  enforced in the handler". There is no such requirement, and the comment teaches the next
  reader something false.

What is required:

- Enrollment is accepted for any authenticated user account. The owner is `user.user_id`.
  Remove the `team_id` gate; do not replace it with a team check.
- Documentation, installer text, code comments and error messages must say "user account" and
  "the user's own key". A reader must never be told to create or use a team-scoped key.

`/login` is not the mechanism here. It exists to mint a UI session JWT for the dashboard and
returns a cookie, not an API credential, so it is unusable by a connector. The user's account
key is the credential the binary sends.

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

- The binary performs the registration and enrollment calls itself, on behalf of the user whose
  key it was configured with. A user on a source host runs the connector, and the connector
  talks to the gateway; the user does not curl gateway endpoints.
- The binary obtains its own connector identity. A human must never need the broker pepper, and
  must never hand-assemble `connector.env` or the connector identity file.
- The gateway registration routes stop being a human-facing surface. Direct invocation is an
  admin action, and should be authorised as one rather than being reachable by any user with a
  key.

The two requirements fit together like this. The account comes from the deployment manifests, and
its virtual key reaches the source host as the connector's configuration, which is the one thing
an operator does hand over. Everything after
that is the connector's job: it enrolls with the user's key, registers its worker, obtains its
connector identity from the gateway rather than from the pepper, and dials the tunnel. The user
never calls a route, never sees the pepper, and never learns a second credential.

Explicitly not a requirement: adding a new bespoke registration protocol. The existing gateway
routes are the right mechanism. What changes is who calls them, and under whose credential.

## What this supersedes

These statements, written earlier the same day, were wrong. The documentation ones are already
corrected in place as of this record; they are listed so a reader of an older revision knows why:

- `byom-self-service.md`: "the connector authenticates enrollment with a team-scoped virtual
  key" and the claim that the bootstrap question is closed by that. Corrected.
- `running-system/README.md`: the Status register row for bootstrap credential exchange marked
  DONE, and the paragraph describing the team-scoped key as the answer. Corrected, and the
  enrollment section now names the deployed user account as the identity.
- `lifecycle-and-pooling-questions.md`: §2 declared closed on a team-scoped key. Corrected to
  partly answered and still open.
- `source_agent_distribution.py` module docstring, which stated the team-scoped key shape.
  Corrected; it now says the account comes from deployment and the connector uses that user's key.
- `route_checks.py`, whose `/mcp-sources` comment named a team-scope requirement that does not
  and must not exist. Corrected.

The code defects those statements described are not corrected and are what this file is for: the
`team_id` gate in `enroll()`, the missing connector registration subcommand, and the pepper-only
path to a connector identity.

The flow sketched in `byom-self-service.md` (step 2, "connector calls `POST /mcp-sources` with
`Authorization: Bearer <team-key>`") was directionally right about the connector making the
call and wrong about the credential being team-scoped. Requirement 2 keeps the call site and
changes the credential.

## What remains true and is not in scope here

## Scope: exactly two requirements, nothing else moves

These are the only two things discovered in the 2026-11-18 walkthrough that need building. Who
creates the account, and who makes the registration call. Everything else in the flow stays
exactly as it is today, and in particular the approval gate does not change.

**The gated approval of newly registered MCP entries stays the same.** A source that the
connector enrolls still lands in `pending_review` and still serves nothing until an
administrator approves it. The connector enrolls under the user's key and opens its tunnel, and
the tunnel opening does not grant access: pending and rejected rows are structurally absent from
the MCP registry, so no session exists against them and no dispatch can reach the source.
Approval, guardrail editing, and rejection remain the gateway administrator's actions through
`GET /v1/mcp/server/submissions`, `PUT /v1/mcp/server/{id}/approve`, and the existing admin
surface. An approval-free path is not wanted and must not be introduced as a side effect of
either requirement above.

The same holds for the rest of the surface: the loopback-only target, the catalog-fingerprint
probe, generation fencing, revocation, and header stripping are implemented, were verified live,
and are not reopened. Requirement 2 changes which process calls the registration routes and under
whose credential. It does not change what those routes decide, what they return, or what review
they submit to.

The two FastAPI services and two MCP servers stood up on the source host for the acceptance run
are fixtures, not part of the product. Only the OpenAPI-shaped services can be carried by the
broker, because `canonical_spec` requires an `openapi: 3.x` document with `paths`; a native MCP
server has no such document and is rejected. That asymmetry is expected and unrelated to these
requirements.

## Acceptance for these requirements

A deployed user account, on a host the cluster cannot reach and with no cluster access and no team
membership, is configured with that account's own key and runs the connector. Without an
administrator touching the cluster, and
without the user calling any gateway route by hand, the source appears for review owned by
that user account, opens its tunnel, and after approval serves a real read. The user never sees
the pepper, never writes `connector.env` by hand, and never derives a credential. The connector
authenticates the whole exchange with the user's own key, and the gateway audit trail shows the
calls arriving as that user, not as the admin.

The same run must also show the approval gate unchanged: after the connector enrolls and its
tunnel is up, the source is still absent from the MCP registry and a tool call against it still
fails, and only an administrator approving it makes the read succeed. A run that serves before
approval has regressed the gate and does not count as passing.

Until that is demonstrated, developer acceptance testing of this broker is blocked.
