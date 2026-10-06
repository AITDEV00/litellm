#!/usr/bin/env python3
"""Provision the OICM Keycloak service account the status controller authenticates as.

Idempotent, so re-running it is always safe. It creates the user when missing,
completes the user profile, sets the password, assigns the client roles, then
proves the account works by issuing a token and calling the routes the
controller actually reads.

Run it as a Kubernetes Job (``provision-service-account-job.yaml``) so that
provisioning is a manifest instead of a hand-run command. Every input comes from
the environment, so the same script serves either cluster.

Why the profile step exists: Keycloak's declarative user profile marks ``email``
required for the ``user`` role in these realms. A user created without an email
has ``requiredActions: []`` and looks complete, but the password grant fails
with ``invalid_grant: Account is not fully set up``. Checking ``requiredActions``
alone therefore gives a false all-clear, so the email is always set here.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ROLES = ("admin", "default", "rsc_groups_full_access")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"ERROR: required environment variable {name} is not set")
    return value


def call(
    method: str,
    url: str,
    token: str | None = None,
    body: dict | None = None,
    form: dict | None = None,
) -> tuple[int, bytes]:
    headers: dict[str, str] = {}
    data: bytes | None = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=30)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        # Connection and DNS failures. Returned as a status of 0 so callers
        # report them the same way as an HTTP error.
        return 0, str(exc).encode()


def main() -> int:
    kc = env("KC_BASE_URL").rstrip("/")
    admin_user = env("KC_ADMIN_USER")
    admin_password = env("KC_ADMIN_PASSWORD")
    realm = env("KC_REALM")
    client_id = env("KC_CLIENT_ID")
    sa_user = env("SA_USERNAME")
    sa_password = env("SA_PASSWORD")
    sa_email = env("SA_EMAIL", f"{sa_user}@{realm}.local")
    roles = tuple(env("SA_ROLES", " ".join(DEFAULT_ROLES)).split())
    api_url = env("OICM_API_URL", "").rstrip("/")
    workspace_id = env("OICM_WORKSPACE_ID", "")

    print(f"target      : {kc}  realm={realm}  client={client_id}")
    print(f"service acct: {sa_user}  roles={list(roles)}")

    # 1. admin token
    status, raw = call(
        "POST",
        f"{kc}/realms/master/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": admin_user,
            "password": admin_password,
        },
    )
    if status != 200:
        print(f"FAIL admin token -> {status} {raw[:300].decode(errors='replace')}")
        return 1
    admin_token = json.loads(raw)["access_token"]
    print("ok   admin token")

    # 2. resolve the client uuid
    status, raw = call(
        "GET",
        f"{kc}/admin/realms/{realm}/clients?clientId={urllib.parse.quote(client_id)}",
        admin_token,
    )
    clients = json.loads(raw) if status == 200 else []
    if not clients:
        print(f"FAIL client {client_id!r} not found in realm {realm!r} (status {status})")
        return 1
    client_uuid = clients[0]["id"]
    if not clients[0].get("directAccessGrantsEnabled"):
        print(f"FAIL client {client_id!r} has directAccessGrantsEnabled=false, password grant unavailable")
        return 1
    print(f"ok   client uuid {client_uuid} (direct access grants enabled)")

    # 3. get or create the user
    status, raw = call(
        "GET",
        f"{kc}/admin/realms/{realm}/users?username={urllib.parse.quote(sa_user)}&exact=true",
        admin_token,
    )
    existing = json.loads(raw) if status == 200 else []
    if existing:
        user_id = existing[0]["id"]
        print(f"ok   user exists ({user_id})")
    else:
        status, raw = call(
            "POST",
            f"{kc}/admin/realms/{realm}/users",
            admin_token,
            body={"username": sa_user, "enabled": True, "emailVerified": True},
        )
        if status not in (201, 204):
            print(f"FAIL create user -> {status} {raw[:300].decode(errors='replace')}")
            return 1
        status, raw = call(
            "GET",
            f"{kc}/admin/realms/{realm}/users?username={urllib.parse.quote(sa_user)}&exact=true",
            admin_token,
        )
        found = json.loads(raw)
        if not found:
            print("FAIL user was created but cannot be read back")
            return 1
        user_id = found[0]["id"]
        print(f"ok   user created ({user_id})")

    # 4. complete the profile (the email requirement described in the docstring)
    status, raw = call(
        "PUT",
        f"{kc}/admin/realms/{realm}/users/{user_id}",
        admin_token,
        body={
            "email": sa_email,
            "emailVerified": True,
            "firstName": "LiteLLM",
            "lastName": "Controller",
            "enabled": True,
            "attributes": {},
        },
    )
    if status not in (204, 200):
        print(f"FAIL complete profile -> {status} {raw[:300].decode(errors='replace')}")
        return 1
    status, raw = call("GET", f"{kc}/admin/realms/{realm}/users/{user_id}", admin_token)
    profile = json.loads(raw)
    if not profile.get("email") or not profile.get("emailVerified"):
        print(f"FAIL profile still incomplete: email={profile.get('email')!r} verified={profile.get('emailVerified')!r}")
        return 1
    if profile.get("requiredActions"):
        print(f"FAIL user still has required actions: {profile['requiredActions']}")
        return 1
    print(f"ok   profile complete (email={sa_email}, no required actions)")

    # 5. password
    status, raw = call(
        "PUT",
        f"{kc}/admin/realms/{realm}/users/{user_id}/reset-password",
        admin_token,
        body={"type": "password", "value": sa_password, "temporary": False},
    )
    if status not in (204, 200):
        print(f"FAIL reset password -> {status} {raw[:300].decode(errors='replace')}")
        return 1
    print("ok   password set (non-temporary)")

    # 6. roles
    status, raw = call("GET", f"{kc}/admin/realms/{realm}/clients/{client_uuid}/roles", admin_token)
    available = {role["name"]: role for role in json.loads(raw)}
    missing = [name for name in roles if name not in available]
    if missing:
        print(f"FAIL client {client_id!r} is missing roles {missing}; available={sorted(available)}")
        return 1
    status, raw = call(
        "POST",
        f"{kc}/admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_uuid}",
        admin_token,
        body=[available[name] for name in roles],
    )
    if status not in (204, 200):
        print(f"FAIL assign roles -> {status} {raw[:300].decode(errors='replace')}")
        return 1
    status, raw = call(
        "GET",
        f"{kc}/admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_uuid}",
        admin_token,
    )
    granted = sorted(role["name"] for role in json.loads(raw))
    if not set(roles).issubset(granted):
        print(f"FAIL roles not fully granted: want={sorted(roles)} got={granted}")
        return 1
    print(f"ok   roles granted: {granted}")

    # 7. prove the password grant works
    status, raw = call(
        "POST",
        f"{kc}/realms/{realm}/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": client_id,
            "username": sa_user,
            "password": sa_password,
            "scope": "openid",
        },
    )
    if status != 200:
        print(f"FAIL password grant -> {status} {raw[:300].decode(errors='replace')}")
        return 1
    token = json.loads(raw)["access_token"]
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    granted_claims = sorted((claims.get("resource_access") or {}).get(client_id, {}).get("roles", []))
    if not set(roles).issubset(granted_claims):
        print(f"FAIL token lacks roles: want={sorted(roles)} got={granted_claims}")
        return 1
    print(f"ok   password grant returned a token carrying {granted_claims}")

    # 8. prove the token reaches the routes the controller reads
    if api_url and workspace_id:
        routes = (
            f"/api/v1/workspaces/{workspace_id}/deployment_summary",
            f"/api/v1/workspaces/{workspace_id}/deployments",
            "/api/entities/workspace",
        )
        for route in routes:
            status, raw = call("GET", api_url + route, token)
            if status != 200:
                print(f"FAIL GET {route} -> {status} {raw[:200].decode(errors='replace')}")
                return 1
            print(f"ok   GET {route} -> 200")
    else:
        print("skip API route checks (OICM_API_URL / OICM_WORKSPACE_ID not set)")

    print("\nDONE: service account is provisioned and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
