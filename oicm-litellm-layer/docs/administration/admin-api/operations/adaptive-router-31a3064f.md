# adaptive_router: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /adaptive_router/state` | **`get_adaptive_router_state_adaptive_router_state_get`**<br>`Get Adaptive Router State`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: unspecified` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/proxy_server.py#L21073 - get_adaptive_router_state()` | `Operation ID and function AST mapped. Response is function-returned, or contract is declared at parent wrapper.` |
