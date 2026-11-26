# OICM → LiteLLM Layer: quick navigator

Start here to find the owning document for a task. Do not search by file number or follow an old investigation before checking its date and status

Code and deployed configuration are authoritative. Read [Documentation trust and ownership](document-status.md) before treating a doc as a current production contract, especially a plan, measurement, or dated deployment observation

## Find the doc by task or symptom

| Task, symptom, or keyword | Open first | Then |
|---|---|---|
| Multiple teams, user membership, access groups, MCP permissions | [Teams and access-group playbook](admin-api/TEAM-AND-ACCESS-GROUP-PLAYBOOK.md) | [MCP permission layers](access-control/permissions-and-scope.md), [RBAC](access-control/rbac-and-full-potential.md) |
| SSO, Entra, JWT, application authentication, delegated team selection | [SSO application integration](access-control/sso-application-integration.md) | [Authentication alternatives](access-control/sso-vs-app-auth.md) |
| Key ownership, user rebind, transfer, historical spend | [Key reassignment](access-control/key-reassignment.md) | [Admin REST key operations](admin-api/LITELLM-ADMIN-REST-API.md) |
| Admin endpoint, role, API schema, request body | [Admin REST reference](admin-api/LITELLM-ADMIN-REST-API.md) and [capability map](admin-api/ADMIN-CAPABILITY-MAP.md) | [Complete operation register](admin-api/OPERATION-REGISTER.md) for all routes, not proof they are enabled |
| Call a model, TTS, STT, image, or reasoning model | [Gateway usage guide](usage-guides/GATEWAY_GUIDE.md) | Provider guides in the [docs map](docs-map.md#usage-guides) |
| MCP source, connector, enrollment, OAuth, tool dispatch | [Source broker running record](mcp-source-broker/running-system/README.md) | [MCP architecture index](mcp/architecture-research-index.md), [source connector guide](usage-guides/MCP_SOURCE_AGENT_GUIDE.md) |
| MCP tool, schema, parameter, result scanning, denied tools | [MCP permission layers](access-control/permissions-and-scope.md) | [Guardrailing](mcp/guardrailing.md) |
| OpenCode, private skills, MCP sign-in | [OpenCode pilot plan and status](mcp/opencode-sso-oauth-skills-pilot.md) | [Dated pilot evidence](mcp/opencode-pilot-evidence.md); unfinished gates are not capability claims |
| Change password, master key, or secrets | [Credentials and secrets](credentials.md) | Follow its rotation contract, do not change only one component |
| Deploy, roll out, report on dev, query cluster | [Deployment](operations/deployment.md) and [datasource validation](runbooks/DATASOURCE-LOCAL-VALIDATION.md) | [Dev tunnel rule](document-status.md#what-must-remain-true) |
| Gateway OOM, spend analytics, performance regression | [Performance evidence index](docs-map.md#performance) | [Incidents](docs-map.md#incidents); verify deployment state before changing code |
| Discovery controller, registered models, pricing, OICM status | [Controller](discovery-controller/README.md) and [component docs](components/controller.md) | [Model pricing](model-pricing/PRICING-LOGIC-MAP.md), [OICM status map](docs-map.md#oicm-status) |
| Custom-provider implementation or audio payloads | [Custom-provider index](custom-providers/README.md) | [Endpoint architecture](custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md) |
| Install CLI or source agent, artifact distribution | [Client distribution plan](client-distribution/PLAN.md) | Its deployment, security, and history pages |
| Reusable coding or forensic method | [Techniques index](docs-map.md#techniques) | [Structure](structure.md) for the wider repo layout |
| Add or move documentation | [Documentation ownership process](document-status.md#discovery-maintenance) | [Complete inventory](docs-map.md) |

## Dev versus prod is a route distinction, not a documentation detail

The dev gateway has no ingress. Use `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000` and re-run it after a dev rollout. `litellm.ecouncil.ae` fronts production and the dev broker tunnel path, not the dev gateway, so a request through the public host is not a dev-gateway proof

## Adding information for a later agent

Put new behavior guidance in this tree next to its owning component and record its status in its index. Use `docs/AGENTS.md` for the maintenance contract. Do not create a new "Q&A" scratch directory or recreate the retired `mcp-architecture-qa/`; current knowledge must stay findable by behavior, not the conversation that produced it

Reports generated from the database, OpenAPI or other live inventories should retain the extraction date. Keep these as evidence or snapshot pages, rather than silently turning them into production guarantees

## Layout

```
oicm-litellm-layer/
├── mkdocs.yml          ← this site's config (edit to add nav pages)
├── docs/               ← this documentation's source markdown
├── controller/         ← discovery controller (component #1)
├── config/             ← LiteLLM proxy configs
├── hooks/              ← LiteLLM callbacks / hooks (components #3, #4)
├── deploy/             ← Kubernetes manifests (apply these)
├── examples/usage/     ← runnable usage tests for gateway endpoints
└── ...                 ← full map on the Structure page
```