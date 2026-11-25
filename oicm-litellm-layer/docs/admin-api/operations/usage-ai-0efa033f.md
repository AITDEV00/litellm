# usage_ai: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `POST /usage/ai/chat` | **`usage_ai_chat_usage_ai_chat_post`**<br>`Usage Ai Chat`<br>`body application/json: UsageAIChatRequest (object)` | `200: Successful Response; application/json: unspecified`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/usage_endpoints/endpoints.py#L40 - usage_ai_chat()` | `Operation ID and function AST mapped. Response is function-returned, or contract is declared at parent wrapper.` |
