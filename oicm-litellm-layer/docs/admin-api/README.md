# Admin surface reference

Use this directory for management routes, role boundaries, and administrative task guidance. Identity mechanisms belong in [Access control](../access-control/README.md); the source broker belongs to [MCP running system](../mcp-source-broker/running-system/README.md)

| Document | Use for | Trust |
|---|---|---|
| [Full admin REST API](LITELLM-ADMIN-REST-API.md) | Management endpoint request fields, examples, role notes | Current reference for the checked gateway version |
| [Admin capability map](ADMIN-CAPABILITY-MAP.md) | UI operations, default-user provisioning, deployment caveats, and handler-level checks | Current reference plus 2026-11-25 observations |
| [Teams and access-group playbook](TEAM-AND-ACCESS-GROUP-PLAYBOOK.md) | Provisioning and offboarding users across teams with resource grants | Current reference; validate proposed production changes on dev |
| [Athena read-only key guide](ATHENA-READ-ONLY-KEY-GUIDE.md) | Narrow administrative read-only account and its allowed operations | Deployment-specific reference |
| [OpenAPI operation register](OPERATION-REGISTER.md) | All advertised paths and methods for the snapshot release | Generated snapshot for 1.108.5, dated 2026-11-25 |

`operations/` is generated inventory for [the operation register](OPERATION-REGISTER.md), not a task checklist or an inventory of permissions. Do not manually modify those generated pages. Presence of a route in OpenAPI does not establish that its underlying provider integration or permission is configured

For most team, key, and resource-grant tasks, source inspection and latency matters less than precise identity scoping: a virtual key selects one `team_id`. Do not infer a person's team memberships are combined into another team's key
