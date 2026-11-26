# experimental: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `GET /sso/readiness` | **`sso_readiness_sso_readiness_get`**<br>`Sso Readiness`<br>No explicit query/path/header parameters or request body in this operation. Required headers supplied by gateway auth are in actual source | `200: Successful Response; application/json: unspecified` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/ui_sso.py#L2916 - sso_readiness()` | `Operation ID and function AST mapped. Response is function-returned, or contract is declared at parent wrapper.` |
