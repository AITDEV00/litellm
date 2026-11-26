# [beta] MCP: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /public/mcp_hub` | **`get_mcp_servers_public_mcp_hub_get`**<br>`Get Mcp Servers`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: array[MCPPublicServer (object)]` | `Operation has no explicit local security override; check parent-handler auth and gateway route gates. Do not treat absent metadata as public access.` | `litellm/proxy/public_endpoints/public_endpoints.py#L347 - get_mcp_servers()` | `Operation ID and function AST mapped. This decorator does not itself name user_api_key_auth. Check parent router/app or non-handler API auth, not anonymous.` |
