# Cost Optimization: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /cost_optimization/prompt_caching/requests` | **`get_prompt_caching_requests_cost_optimization_prompt_caching_requests_get`**<br>`Get Prompt Caching Requests`<br>`start_date query required: string`<br>`end_date query required: string`<br>`page_size query: integer`<br>`filter query: string`<br>`cursor_start_time query: string or null`<br>`cursor_request_id query: string or null` | `200: Successful Response; application/json: PromptCachingRequestsResponse (object)`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/prompt_caching_requests.py#L132 - get_prompt_caching_requests()` | `Operation ID and function AST mapped. This decorator does not itself name user_api_key_auth. Check parent router/app or non-handler API auth, not anonymous.` |
