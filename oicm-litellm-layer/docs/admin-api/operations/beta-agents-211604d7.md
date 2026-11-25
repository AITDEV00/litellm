# [beta] Agents: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /public/agent_hub` | **`get_agents_public_agent_hub_get`**<br>`Get Agents`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: array[AgentCard (object)]` | `Operation has no explicit local security override; check parent-handler auth and gateway route gates. Do not treat absent metadata as public access.` | `litellm/proxy/public_endpoints/public_endpoints.py#L323 - get_agents()` | `Operation ID and function AST mapped. This decorator does not itself name user_api_key_auth. Check parent router/app or non-handler API auth, not anonymous.` |
