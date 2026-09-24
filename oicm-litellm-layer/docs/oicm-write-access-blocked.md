# OICM `POST /api/v1/model_servers` blocked (403) — root-cause findings

Date: 2026-09-23
Cluster: ADEO AI Engine (ecouncil.ae), namespace `oicm-keycloak` + `mlops`
Status: OICM API **read works**, **write blocked** by two independent platform defects. No cluster state was changed (test objects removed).

## Summary

Onboarding scripts can authenticate and **read** model servers over the OICM REST API, but cannot **create** them. `oicm-admin` returns `{"error_code": 403, "message": "UNAUTHORIZED ACCOUNT ACTIVITY"}` on `POST /api/v1/model_servers`. The intended write account `app-admin` does not exist. Neither defect is fixable by the onboarding scripts.

## Authentication (now working)

The OICM backend re-validates every Bearer token by POSTing to Keycloak `userinfo` over the **internal** service URL. Keycloak only accepts tokens whose `iss` host matches `KC_HOSTNAME`. Two issuer hosts exist:

| Issuer | userinfo via internal svc | Result |
|---|---|---|
| `https://auth.adeoaiengine.ecouncil.ae` | HTTP 401 | `{"error_code":401,"message":"IDP ERROR: , code: 401"}` |
| `https://auth-oicm.adeoaiengine.ecouncil.ae` | HTTP 200 | API returns data (HTTP 200) |

Fix for scripts: use `AUTH_BASE_URL=https://auth-oicm.adeoaiengine.ecouncil.ae`. Verified: `GET /api/v1/model_servers/summary` returns HTTP 200 with full data.

## Defect A — `app-admin` / `application_admin` never provisioned

`oicm-keycloak-initializer-config-secret` (`oicm-keycloak` ns) declares two applications:

```yaml
applications:
  - name: "admin"
    admin_username: "oicm-admin"
    admin_role_name: "super_admin"
    admin_password: <redacted - see oicm-keycloak-initializer-config-secret>
  - name: "admin"
    admin_username: "app-admin"
    admin_role_name: "application_admin"
    admin_password: <redacted - see oicm-keycloak-initializer-config-secret>
```

Evidence it's orphaned / only the first entry effective:

- No pod, job, env var, or volume in **any** namespace references `oicm-keycloak-initializer-config-secret`. Scanned all pod `spec.volumes` and container `env` cluster-wide — zero matches.
- The only initializer workload is `job/keycloak-initializer-job` in namespace `keycloak` (status `Complete`, age ~282d). It reads `keycloak-secret` / `keycloak-initializer-config-secret` and targets the **other** Keycloak (`auth.adeoaiengine...`), not the `auth-oicm.` one the backend validates against.
- Keycloak `admin` realm contents (via master admin API):
  - Users: only `oicm-admin` (id `26cac0ce-36d9-422c-8c3f-e99e1f1b0ef2`, enabled=true). `GET /admin/realms/admin/users?username=app-admin` returns `[]`.
  - `admin` client roles: `rsc_groups_full_access`, `default`, `super_admin`. No `application_admin`. Realm roles: only `default-roles-admin`, `uma_authorization`, `offline_access`.
- `app-admin` / `application_admin` not found in **any** realm (`genaiadmin, admin, oichat, master, oitenant1, adeo, test, oitest`).

## Defect B — new users cannot use the direct password grant

Even if `app-admin` were created, it could not log in:

- Created `probe1` and `app-admin` via the admin REST API with `credentials:[{type:password,...}]` and again via `PUT .../reset-password` (HTTP 204). Every `grant_type=password` attempt returned `{"error":"invalid_grant","error_description":"Invalid user credentials"}` against both the external `auth-oicm.` issuer and the internal cluster service.
- The seeded `oicm-admin` succeeds with the identical password-grant call.
- Keycloak pods emitted **no** auth-error log entries for the failing attempts, consistent with realm-level user-profile/grant restriction that only initializer-provisioned users satisfy.
- Realm has `bruteForceProtected: false`, so it is not lockout.

Test objects (`probe1`, `app-admin`, `application_admin` role) were deleted after testing (HTTP 204); cluster is back to baseline.

## Why `oicm-admin` gets 403

`oicm-admin` authenticates (token verified, `resource_access.admin.roles=["super_admin"]`) but the write endpoint's IAM data authorizer rejects it. Backend traceback:

```
flask_jwt_extended verify_jwt_in_request
  → auth.py verify_token → BearerTokenVerifier.verify_bearer_token   (auth OK)
  → app/http/app/auth/decorators.py:67 wrapper                        (authorization)
  → .../iam DataAuthorizerHelper → raise GlobalException 403 "UNAUTHORIZED ACCOUNT ACTIVITY"
```

So `super_admin` is read-only for `model_servers` in this deployment's IAM policy. The role the platform intends for write is `application_admin`, which is absent.

## What the platform team must do

1. Provision the `application_admin` client role on the `admin` client and the `app-admin` user in the `admin` realm **through the same initializer mechanism that created `oicm-admin`** (so the direct password grant works), per the already-present (but orphaned) `oicm-keycloak-initializer-config-secret`. The two `applications` entries currently share the identical realm key `name: "admin"`; if the initializer de-duplicates on that key, give the second entry a distinct reconciliation path or merge both users into one `applications` item with a `users:`/`roles:` list.
2. Ensure the `oicm-keycloak` initializer config is actually consumed (a mounted init job or reconciler bound to `auth-oicm.`), because today nothing reads it.
3. Confirm the backend IAM policy grants `application_admin` the create/write permission on `model_servers` so `POST /api/v1/model_servers` returns 201 instead of 403.

## Reproduction

```bash
# read works
TOK=$(curl -sk -X POST https://auth-oicm.adeoaiengine.ecouncil.ae/realms/admin/protocol/openid-connect/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'client_id=admin&username=oicm-admin&password=<from oicm-keycloak-initializer-config-secret>&grant_type=password&scope=openid' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -sk -H "Authorization: Bearer $TOK" https://oicm.adeoaiengine.ecouncil.ae/api/v1/model_servers/summary   # 200

# write blocked
curl -sk -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"name":"probe","tag":"0.0.1","family":"vLLM","devices":{"nvidia":"x"},"task_types":["Text Generation"],"server_arguments":[],"inference_arguments":[],"tasks_base_url":[],"enabled":true,"health_check_endpoint":"/health","enable_metrics":true}' \
  https://oicm.adeoaiengine.ecouncil.ae/api/v1/model_servers   # 403 UNAUTHORIZED ACCOUNT ACTIVITY
```

Passwords are redacted above; the real values live in the `oicm-keycloak-initializer-config-secret` in the `oicm-keycloak` namespace and must not be committed.
