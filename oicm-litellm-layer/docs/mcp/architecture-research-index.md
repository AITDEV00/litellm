# MCP architecture and source broker research

Reviewed on 2026-11-24. This folder holds MCP runtime, source-broker architecture, and OpenCode integration material. Authorization, keys, and SSO now live in [Identity and access control](../access-control/sso-application-integration.md)

## Start here

| Read | Use it for | Status |
|---|---|---|
| [Running source broker](../mcp-source-broker/running-system/README.md) | Owning record for the deployed source-broker system | Current reference unless deployed state differs |
| [Topology](source-broker-topology.md) | Components, ports, connections, and enrollment | Implementation notes; manifests and deployed state win where snapshots differ |
| [Runtime flow](source-broker-runtime-flow.md) | Dial home, admission, probing, dispatch, reconciliation, and revocation | Implementation notes |
| [Client install](client-install.md) | Installing the source connector and what it writes | Implementation notes |
| [Production cluster](prod-cluster.md) | What was already running in prod before a user acts | Dated deployment observation |
| [File map](source-broker-file-map.md) | Relevant source files and manifests | Source index |
| [MCP guardrailing](guardrailing.md) | Parameter allowlists, scanning, and trust-state tool denial | Current mechanism reference; deployment counts are dated observations |
| [OpenCode pilot plan](opencode-sso-oauth-skills-pilot.md) | Native MCP OAuth and private-skill pilot procedure | Plan with explicit gates, not completed capability claims |
| [OpenCode pilot evidence](opencode-pilot-evidence.md) | What was run on dev and prod on 2026-11-24 | Point-in-time evidence; it records unfinished credential and skill-loading gates |

For current operations, do not repeat old recommendations to disable public MCP sources or postpone SSO. Remote sources need public-address eligibility for their users, and Microsoft OIDC/browser SSO is deployed. Team membership never merges all of a user's teams into one virtual key

## Authorization and identity moved

- [Permissions and tool scope](../access-control/permissions-and-scope.md): the route, server, tool, parameter, and guardrail layers
- [RBAC and full potential](../access-control/rbac-and-full-potential.md): role boundaries and resource ceilings
- [Key reassignment](../access-control/key-reassignment.md): ownership rebinding and spend history
- [SSO application integration](../access-control/sso-application-integration.md): delegated-application provisioning, subject to its marked rollout checks
- [Temporary key pattern](../access-control/sso-temporary-key-pattern.md): delegated authority for transient application credentials

The old `mcp-architecture-qa/` folder was retired during the 2026-11-26 documentation reorganization. Do not add files to it

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
