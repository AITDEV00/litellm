# One capability layer behind any agent client — fit assessment

Status: assessment recorded 2026-11-17 against branch `jya0-v1.108.5`. No new code.
Reviews the director's platform vision against what the integration layer and the MCP source
broker already implement, and names the gaps that remain.

## Problem statement (as stated by the director)

> We can give them agents on their machines like open code or something restrict certain tool usage
> or give them a virtual environment which have all type of agents whatever they want to use. And
> whenever there is something new we can just add it to this virtual environment. They can play
> around with it use it test it.
>
> In an ideal situation we want one layer which have the MCPs the tools the skills the models and
> then sync it with Active Directory to create a user per employee and then whatever agents they
> use they can just plug into it.

Restated as three planes:

- Identity plane: one user per employee, synced from Active Directory, every action attributable
  and revocable
- Capability plane: one layer holding models, MCP servers, tools, and skills, centrally curated;
  adding a thing once makes it available to everyone entitled to it
- Client plane: whatever agent an employee prefers (OpenCode, Claude Code, Codex, Cursor, ...),
  running on their machine or in a workspace, plugged into the layer

## Why the current architecture is the better shape for this

The naive plan is "install a powerful agent on each machine and give it API keys and tool
configs." That plan fails at the four points an enterprise cares about: every laptop holds a real
provider credential, every capability change touches N machines, there is no per-employee audit
or budget, and offboarding is best-effort.

The implemented inversion fixes all four structurally. The machine holds exactly one revocable
virtual key carrying the employee's identity. The agent is a thin shell; models, tools, budgets,
guardrails, and audit live in one controlled layer. A capability is added once and a revoked key
fully disables a person. And the piece most agent platforms cannot do at all, the MCP source
broker turns internal services on private hosts into tools in that same layer through an
outbound-only connection, so the capability plane includes company systems, not just public
services. The security property that falls out is "untrusted client, trusted center": any agent
on any machine is treated as untrusted and can reach only what its key says, with per-user
logging and post-call guardrails.

## Element-by-element fit

| Vision element | What exists today | Fit |
|----------------|------------------|-----|
| One layer for all models | Gateway model registry; the discovery controller continuously registers deployments and `/v1/models` serves authorized keys. Verified live with 28 models | Direct |
| One layer for all tools and MCPs | MCP server manager with per-server access groups; the source broker (reverse-h2 transport, verified on dev 2026-11-16) exposes private origin services as MCP tools with no inbound port on the source host | Direct |
| Restrict certain tool usage | Four enforced levels: route allowlists per key type, per-key model allowlists, per-key MCP access groups and tool permissions, post-call guardrail scanning. Verified live: an unscoped key receives 403, a scoped key receives exactly its tools | Direct |
| Per-employee identity from AD | Keycloak already federates Active Directory (the `ad-oicm-keycloak-admin` service account exists); the gateway supports Keycloak/OIDC SSO with user auto-provisioning on login | Direct, plus optional batch pre-provisioning |
| Per-employee credential | One virtual key per user with its own budget, rate limits, model scope, tool scope, and spend logs; revoking the key fully disables that employee everywhere | Direct |
| Plug in any agent | Every serious agent CLI speaks OpenAI-compatible chat plus MCP over HTTP. Point the agent at the gateway base URL with the employee's key and attach the gateway's MCP endpoints. The backend is agent-agnostic | Direct |
| Add something new once | Enrollments are admin-side; the controller syncs models continuously and MCP tools appear for entitled keys with zero per-machine work | Direct |
| "Virtual environment with all the agents" | A golden client image problem, not a backend problem: a disposable devcontainer with the agent CLIs preconfigured for the gateway. Adding an agent touches the image only | Indirect: supported, not shipped |
| Skills in the one layer | Skills and prompt libraries are agent-side artifacts today. The gateway does not serve them. Path: an artifact-mirror endpoint (rustfs already stores spec artifacts) or a small skills-serving MCP server | Gap, small build |
| Restricting what the agent itself does locally | Out of scope by design: a local CLI's local tools run with the employee's OS permissions. The layer controls everything touching company data and money: models, tools, routes, audit, guardrails. Local tool policy is device management | Boundary, deliberate |

## Recommended build order

1. Golden agent image: a devcontainer/workspace image with the chosen agent CLIs preconfigured
   for the gateway base URL and MCP attach. No backend change
2. Key automation on AD login: wire Keycloak SSO login to per-user key minting so first login
   yields the employee's key without an admin in the loop
3. Skills serving: one MCP server, or an artifact endpoint, that serves the approved skills
   catalog so skills join the layer like everything else
4. Agent profiles: a named template binding models allowed, tools allowed, budget, and rate
   limit, so onboarding an employee is "assign profile" and trying a new agent is "same key,
   new client"

None of these is a redesign; all four sit on contracts already verified live (scoped keys,
access groups, tool permissions, the MCP surface, and the source-broker enrollment flow).

## Honest limits recorded with the source broker work

Multi-host failover is designed for in the pool but has not been exercised with two real hosts;
load beyond tens of concurrent requests is unmeasured on the shipped long-poll protocol; the
dispatch long-poll ties a session to one broker pod, so multi-replica brokering needs a design
decision before scaling; and only the dev overlay exists, production is untouched. These are
tracked in the "Next steps" section of `docs/mcp-source-broker/running-system/README.md`.
