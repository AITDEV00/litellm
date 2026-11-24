# MCP source link: architecture Q&A scratch pad

This folder is a working notebook. It is not an owning document. The owning record for behavior is `../mcp-source-broker/running-system/README.md`, and code wins over every file here.

Use this folder to keep growing answers as questions come up. Each file is one topic, written to be re-read weeks later without the chat.

## Reading order and status

Reviewed on 2026-11-24. This folder mixes implementation notes, dated production observations, and unfinished design work:

| Read | Use it for | Status |
|---|---|---|
| [14](14-sso-application-integration.md) | Current application authentication and identity/team/access-group provisioning | Current guide. Direct delegated-JWT team selection still requires the rollout checks marked in that guide |
| [15](15-opencode-sso-mcp-skills-pilot.md) | Native OpenCode MCP OAuth and private-skill pilot procedure | Plan with explicit gates, not a claim of completed support |
| [16](16-opencode-dev-pilot-evidence.md) | What was actually run on dev and prod on 2026-11-24 | Partial evidence. Native MCP sign-in/discovery works, but model credentials, per-server execution, refresh, and private OpenCode skills are unfinished |
| [01-05](01-topology-diagram.md) | Source-broker architecture and client enrollment | Implementation notes. Manifest and deployed state win where snapshots differ |
| [06-13](06-permissions-and-scope.md) | Authorization investigations and historical findings | Dated research, subject to the later production grants and current SSO configuration recorded here |

For current operations, do not repeat the old recommendations to disable public MCP sources or postpone SSO. Those sources need remote public-address eligibility for their users, and Microsoft OIDC/browser SSO is now deployed. Team membership never merges all of a user's teams into one virtual key

## Files

| File | What it holds |
|---|---|
| [`01-topology-diagram.md`](01-topology-diagram.md) | Every entity, every port, every arrow, and why each arrow exists. The visual map. |
| [`02-step-1-prod-cluster.md`](02-step-1-prod-cluster.md) | Step 1: what is already running in the prod cluster before any user acts. |
| [`03-step-2-client-install.md`](03-step-2-client-install.md) | Step 2: installing the `adma` binary and what it writes to disk. |
| [`04-runtime-flow.md`](04-runtime-flow.md) | Steps 3 to 5: dial home, admit, probe, dispatch a tool call, reconcile and revoke. |
| [`05-file-map.md`](05-file-map.md) | Every source file and manifest named in the answers, with its purpose. |
| [`06-permissions-and-scope.md`](06-permissions-and-scope.md) | Prod authorization behavior and a dated 2026-11-23 inventory predating the current MCP grants, followed by route/server/tool/parameter controls. |
| [`07-mcp-guardrailing.md`](07-mcp-guardrailing.md) | MCP parameter input guardrailing, description-only schema scanning, and whole-tool denial by conversation trust state. Deployment counts are dated observations. |
| [`08-key-reassignment.md`](08-key-reassignment.md) | Key ownership rebinding, its non-admin namespace guard, team/organization authorization boundaries, and why rotation is usually preferable. |
| [`09-rbac-and-full-potential.md`](09-rbac-and-full-potential.md) | Role ladder and grant fields, with current team/SSO recommendations. The opening inventory is a dated pre-provisioning snapshot. |
| [`10-sso-vs-app-auth.md`](10-sso-vs-app-auth.md) | The distinction between browser SSO, its UI session and embedded dashboard key, delegated Entra API JWTs, gateway MCP sessions, and application virtual keys. |
| [`11-sso-temporary-key-pattern.md`](11-sso-temporary-key-pattern.md) | The "app uses SSO, each user mints a temporary key" pattern: how session tokens delegate to keys, the delegated-authority ceiling, the three failure modes, and which of the three token variants to use. |
| [`12-microsoft-saml-airgap.md`](12-microsoft-saml-airgap.md) | Verified: Microsoft SAML with inline `SAML_IDP_METADATA_XML` needs no Microsoft egress, native OIDC does, plus four corrections to the research and the invariants to preserve. |
| [`13-dev-microsoft-sso-deployed.md`](13-dev-microsoft-sso-deployed.md) | Dev Microsoft OIDC deployment evidence through 2026-11-23. The suspected localhost blocker was resolved by the successful browser SSO recorded in guide 16. |
| [`14-sso-application-integration.md`](14-sso-application-integration.md) | Current guide for application developers who already have Entra SSO and for proxy admins configuring organizations, teams, users, and model/MCP access groups. |
| [`15-opencode-sso-mcp-skills-pilot.md`](15-opencode-sso-mcp-skills-pilot.md) | Implementation plan for local OpenCode V2 Microsoft SSO/native MCP OAuth and private skill loading, with separate mandatory dev then prod stages. |
| [`16-opencode-dev-pilot-evidence.md`](16-opencode-dev-pilot-evidence.md) | Run record of the OpenCode pilot: dev D2 core evidence plus the user-driven prod P0/P1 round, replication steps, the model-route credential-type finding, and open items. |

Read 14 for the current application integration design. Read 15 for the OpenCode native harness pilot. Read 16 for what was actually run and proven in the OpenCode pilot so far. Earlier files are dated research and can describe pre-SSO deployment state

## Conventions in these files

- "gateway" means the `litellm-proxy` Deployment, the normal LiteLLM proxy.
- "broker pod" means the `litellm-mcp-broker` Deployment, which has two containers: `broker` (Python) and `source-link` (Go).
- "worker" means the Go binary running inside the broker pod as `/source-link worker`.
- "connector" or "agent" means the Go binary running on the user's machine as `adma connector`.
- Port numbers are always the in-cluster container or Service port unless the line says "public 443".
- `->` means "the direction the request is initiated by the left side". A reply travels back along the same connection unless stated otherwise.

## Source-of-truth pointers to check before trusting anything here

- Prod manifests: `oicm-litellm-layer/deploy/overlays/prod/`
- Public routing: `oicm-litellm-layer/deploy/prod/litellm-ingress.yaml`, `oicm-litellm-layer/deploy/prod/mcp-tunnel-ingress.yaml`
- Go side: `oicm-litellm-layer/mcp-source-link/`
- Python side: `litellm/proxy/mcp_source_broker/`
- Install surface: `litellm/proxy/client_distribution/source_agent_distribution.py`, `oicm-litellm-layer/templates/source-agent-install.sh.tmpl`
