# compliance: OpenAPI method register

Each row is one advertised HTTP method and path from the live OpenAPI snapshot. See [operation-register overview](../OPERATION-REGISTER.md) for evidence and auth limitations before using these as permissions.

| Method and path | Operation and documented input | Documented responses | OpenAPI security declaration | Runtime source mapping | Tracing status |
|---|---|---|---|---|---|
| `POST /compliance/eu-ai-act` | **`check_eu_ai_act_compliance_compliance_eu_ai_act_post`**<br>`Check Eu Ai Act Compliance`<br>`body application/json: ComplianceCheckRequest (object)` | `200: Successful Response; application/json: ComplianceResponse (object)`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/compliance_endpoints.py#L33 - check_eu_ai_act_compliance()` | `Operation ID and function AST mapped. ` |
| `POST /compliance/gdpr` | **`check_gdpr_compliance_compliance_gdpr_post`**<br>`Check Gdpr Compliance`<br>`body application/json: ComplianceCheckRequest (object)` | `200: Successful Response; application/json: ComplianceResponse (object)`<br>`422: Validation Error; application/json: HTTPValidationError (object)` | `APIKeyHeader: (global). Local declaration; runtime Depends(user_api_key_auth) can still require gateway API-key auth.` | `litellm/proxy/management_endpoints/compliance_endpoints.py#L62 - check_gdpr_compliance()` | `Operation ID and function AST mapped. ` |
