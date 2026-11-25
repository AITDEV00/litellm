# a2a_registration: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `POST /v1/a2a/discover` | **`discover_agent_card_v1_a2a_discover_post`**<br>`Discover Agent Card`<br>`body application/json: DiscoverAgentRequest (object)` | `200: Successful Response; application/json: DiscoverAgentResponse (object)`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/a2a/endpoints.py#L73 - discover_agent_card()` | `Operation ID and function AST mapped. ` |
