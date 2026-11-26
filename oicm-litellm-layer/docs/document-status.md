# Documentation trust and ownership

Use this page before following a documented command, especially in research, incident, performance, or evidence files

## What must remain true

Code and the actual deployed deployment are the final authority. A documentation statement can identify a contract, but it does not prove that the corresponding route, feature, model, access group, or permission is currently configured and working

Verify a mutation in dev before production unless an already-approved procedure explicitly says otherwise. A document containing a curl command is not approval to execute it

The dev gateway has no ingress. Use `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000` for a dev proof. `litellm.ecouncil.ae` fronts prod plus the dev broker's tunnel path, not the dev gateway. Re-run the forward after a dev rollout

## Trust levels

| Label used in an index | How to use the document |
|---|---|
| **Current reference** | Its authoritative behavior is code, schema, configuration, or deployed state. Recheck version and configuration before acting on a consequential assertion |
| **Dated deployment observation** | The statements describe what a gateway, cluster, database, user, or grant looked like at a particular date, not its present state. Re-read live state before acting |
| **Plan** | The procedure has not by itself proven support or completion. A prerequisite gate, unfinished work, or required approval still applies |
| **Point-in-time evidence** | A record of actual measurements, tests, audit results, or pilot steps. Preserve it; it does not supersede later changes |
| **Historical** | A closed plan, earlier proposal, completed superseded design, or old investigation. Use it for context and decisions, not as a current operational procedure |
| **Generated (versioned)** | An authenticating OpenAPI extract per release. Do not hand-author edits or infer enabled production features purely because a route appears |

When a document contains multiple statuses, the index should distinguish its code-reference section from historical deployment observations. When it is unclear, treat the document as historical until the owning component evidence is checked

## Ownership directories

| Concern | Start here | Other material |
|---|---|---|
| Teams, users, keys, access grants | [Teams and access groups](administration/admin-api/TEAM-AND-ACCESS-GROUP-PLAYBOOK.md) | [Access control](administration/access-control/sso-application-integration.md), [capability map](administration/admin-api/ADMIN-CAPABILITY-MAP.md) |
| Admin surface and request schemas | [Admin REST API](administration/admin-api/LITELLM-ADMIN-REST-API.md) | [OpenAPI operation register](administration/admin-api/OPERATION-REGISTER.md), whose family pages are generated |
| MCP sources, OAuth, connector, runtime | [Source broker](mcp-source-broker/running-system/README.md) | [MCP research index](mcp/architecture-research-index.md), [source agent guide](usage-guides/MCP_SOURCE_AGENT_GUIDE.md) |
| Calling models and voice APIs | [Gateway guide](usage-guides/GATEWAY_GUIDE.md) | Other provider-specific files under [Usage guides](docs-map.md#usage-guides) |
| Deploy, rollouts, credentials, database | [Deployment](operations/deployment.md) and [Credentials](operations/credentials.md) | [Runbooks](docs-map.md#runbooks) in the docs map |
| Incidents and OOMs | Incident directory by date | [Performance evidence](docs-map.md#performance) in the docs map |
| Controller and OICM status | [Controller](platform/discovery-controller/README.md) and [OICM status](docs-map.md#oicm-status) | API compat under `openrouter/` |
| Client distribute/load sources | [Client distribution](client-distribution/PLAN.md) | Deployment, platform matrix, security, and history in that directory |

## Discovery maintenance

Place a new document with the behavior or component it owns, rather than by authorship date or the task of the person who generated it. Do not recreate the retired `mcp-architecture-qa/` directory

When adding or renaming a document:

1. Update its owning directory index, where one exists, and attach a trust label
2. Update the complete [docs map](docs-map.md)
3. Put the page in `mkdocs.yml` nav unless it is generated inventory under an already-linked register
4. Validate all inbound links and run `oicm-litellm-layer/.venv-docs/bin/mkdocs build`
5. Do not use doc contents alone to prove code is working; test the behavior through the owning application

Preserve dated production facts and the exact date they were read; do not backdate them with the current date during a reorganization. Remove a dated incident report only when it has been deliberately reconciled into the owning reference doc, and still preserve it under Git history
