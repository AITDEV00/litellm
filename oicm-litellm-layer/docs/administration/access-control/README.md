# Identity and access control

Start with [Teams and access-group playbook](../admin-api/TEAM-AND-ACCESS-GROUP-PLAYBOOK.md) for standard administration or [SSO application integration](sso-application-integration.md) when an application uses delegated user identity

A virtual key names exactly one team. Team membership does not union a person's grants into every request. Recheck deployed authorization configuration when acting in production

| Document | What it covers | Trust |
|---|---|---|
| [Permissions and scope](permissions-and-scope.md) | Route, server, tool, parameter, and guardrail enforcement; legacy tags versus unified groups | Current mechanism reference, with 2026-11-23 prod observations |
| [RBAC and full potential](rbac-and-full-potential.md) | Role ladder, org/team budgets, scopes, admin patterns, target model | Current mechanism reference, with a dated opening inventory |
| [Key reassignment](key-reassignment.md) | Ownership rebinding, authorization guard, spend history, possible separate historical correction | Current reference |
| [SSO application integration](sso-application-integration.md) | Entra JWT, delegated team behavior, organizations, teams, and unified access groups | Current guide; heed its rollout prerequisites and actual verification status |
| [SSO vs application auth](sso-vs-app-auth.md) | Browser SSO versus delegated Entra JWTs, gateway MCP sessions, and virtual keys | Current mechanism reference |
| [Temporary key pattern](sso-temporary-key-pattern.md) | SSO session and transient credential authority ceiling and failure modes | Current mechanism reference |
| [Microsoft SAML airgap](microsoft-saml-airgap.md) | SAML metadata, Microsoft egress requirements, and related corrections | Dated deployment research |
| [Dev Microsoft SSO deployed](dev-microsoft-sso-deployed.md) | Dev OIDC deployment, redirect, and verification results | Dated 2026-11-23 deployment observation |

Use the [Complete admin schema reference](../admin-api/LITELLM-ADMIN-REST-API.md) for request shapes rather than inferring parameters from behavior
