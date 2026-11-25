# Router Settings: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /router/settings` | **`get_router_settings_router_settings_get`**<br>`Get Router Settings`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: RouterSettingsResponse (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/router_settings_endpoints.py#L90 - get_router_settings()` | `Operation ID and function AST mapped. ` |
| `GET /router/fields` | **`get_router_fields_router_fields_get`**<br>`Get Router Fields`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: RouterFieldsResponse (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/router_settings_endpoints.py#L173 - get_router_fields()` | `Operation ID and function AST mapped. ` |
