# rust control plane: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `POST /v1/rust_control_plane/logs` | **`ingest_callback_logs_v1_rust_control_plane_logs_post`**<br>`Ingest Callback Logs`<br>`body application/json: CallbackLogsRequest (object)` | `200: Successful Response; application/json: CallbackLogsResponse (object)`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/logging_endpoints/callback_logs_endpoints.py#L187 - ingest_callback_logs()` | `Operation ID and function AST mapped. Response is function-returned, or contract is declared at parent wrapper.` |
