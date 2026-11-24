# MCP source link: architecture Q&A scratch pad

This folder is a working notebook. It is not an owning document. The owning record for behavior is `../mcp-source-broker/running-system/README.md`, and code wins over every file here.

Use this folder to keep growing answers as questions come up. Each file is one topic, written to be re-read weeks later without the chat.

## Files

| File | What it holds |
|---|---|
| [`01-topology-diagram.md`](01-topology-diagram.md) | Every entity, every port, every arrow, and why each arrow exists. The visual map. |
| [`02-step-1-prod-cluster.md`](02-step-1-prod-cluster.md) | Step 1: what is already running in the prod cluster before any user acts. |
| [`03-step-2-client-install.md`](03-step-2-client-install.md) | Step 2: installing the `adma` binary and what it writes to disk. |
| [`04-runtime-flow.md`](04-runtime-flow.md) | Steps 3 to 5: dial home, admit, probe, dispatch a tool call, reconcile and revoke. |
| [`05-file-map.md`](05-file-map.md) | Every source file and manifest named in the answers, with its purpose. |
| [`06-permissions-and-scope.md`](06-permissions-and-scope.md) | Live prod state read-only (8 MCP servers, 10 keys, 25 teams, 0 guardrails), the five access layers, findings, best practice, and MCP parameter guardrailing. |
| [`07-mcp-guardrailing.md`](07-mcp-guardrailing.md) | MCP parameter input guardrailing (`allowed_params`, scanner hook) and whole-tool denial by conversation trust state (`tool_policy`), plus what is not native. |
| [`08-key-reassignment.md`](08-key-reassignment.md) | Whether an existing API key can be reassigned to another user, the admin-only guard, and what a rebind does and does not change. |
| [`09-rbac-and-full-potential.md`](09-rbac-and-full-potential.md) | Is admin-minted-keys the right model, the role ladder (org_admin/team_admin/viewer), the full granularity list, recommended target model, MCP connector promotion, SSO plus airgap, and the done-next sequence. |
| [`10-sso-vs-app-auth.md`](10-sso-vs-app-auth.md) | What Microsoft SSO actually issues (a UI JWT, not a virtual key) and the three ways an application above LiteLLM should authenticate: virtual key, JWT auth federation, or MCP OAuth. |
| [`11-sso-temporary-key-pattern.md`](11-sso-temporary-key-pattern.md) | The "app uses SSO, each user mints a temporary key" pattern: how session tokens delegate to keys, the delegated-authority ceiling, the three failure modes, and which of the three token variants to use. |
| [`12-microsoft-saml-airgap.md`](12-microsoft-saml-airgap.md) | Verified: Microsoft SAML with inline `SAML_IDP_METADATA_XML` needs no Microsoft egress, native OIDC does, plus four corrections to the research and the invariants to preserve. |
| [`13-dev-microsoft-sso-deployed.md`](13-dev-microsoft-sso-deployed.md) | What was actually deployed to dev for Microsoft OIDC SSO, the verification results, the exact redirect URI, and the remaining Entra localhost/confidential-client blocker. |

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
