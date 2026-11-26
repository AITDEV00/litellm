# UI Session: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `POST /session/logout` | **`session_logout_session_logout_post`**<br>`Session Logout`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: SessionLogoutResponse (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/session_endpoints.py#L121 - session_logout()` | `Operation ID and function AST mapped. This decorator does not itself name user_api_key_auth. Check parent router/app or non-handler API auth, not anonymous. Response is function-returned, or contract is declared at parent wrapper.` |
