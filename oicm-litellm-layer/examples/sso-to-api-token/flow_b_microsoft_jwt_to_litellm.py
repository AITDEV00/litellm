#!/usr/bin/env python3
# ruff: noqa: T201  -- a CLI exemplar; printing claims and responses is the point
"""Flow B: forward a user's Microsoft token to LiteLLM as the bearer.

This is the enterprise path (`general_settings.enable_jwt_auth: true`). It exists
for exactly one situation: your service already holds a Microsoft token for the
user, and you want LiteLLM to accept that same credential without a second login
or a shared secret.

How it works, verified against litellm/proxy/auth/user_api_key_auth.py:

  - The proxy inspects the bearer. If `enable_jwt_auth` is on and the value
    parses as a JWT, it validates the signature against `JWT_PUBLIC_KEY_URL`
    (the Entra JWKS), then the `aud` against `JWT_AUDIENCE` and `iss` against
    `JWT_ISSUER`.
  - The validated claims are mapped to a user/team/role through
    `general_settings.litellm_jwtauth` field mappings (`user_id_jwt_field`,
    `user_email_jwt_field`, `team_id_jwt_field`, and so on).
  - `premium_user` must be True. The code raises
    "JWT Auth is an enterprise only feature" otherwise.

THE AUDIENCE TRAP, and why this file has two modes:

An Entra token's `aud` is whatever API the token was issued for. If your app and
LiteLLM are separate app registrations, the token your app already holds has the
wrong `aud` for LiteLLM and will be rejected. You have two resolutions:

  Mode 1 (recommended): expose an API scope on the LiteLLM app registration and
  have your service request a token for THAT scope. The `aud` is then the LiteLLM
  app id, which is what you put in JWT_AUDIENCE.

  Mode 2: register LiteLLM's JWT_AUDIENCE as your own app id and use the same app.

A third case, app-only tokens from `client_credentials`: these carry no user
identity at all, only the service principal's `oid`/`sub`, with no `upn`/`email`.
They prove the pipeline but do NOT represent a user. Do not use them to identify
people.

Usage:
    # Inspect any token's claims without calling LiteLLM:
    python3 flow_b_microsoft_jwt_to_litellm.py inspect --token "$TOK"

    # Present that token to LiteLLM as the bearer:
    python3 flow_b_microsoft_jwt_to_litellm.py call --base-url http://localhost:14001 --token "$TOK"

    # Get a real delegated USER token by device code (needs one browser click):
    python3 flow_b_microsoft_jwt_to_litellm.py device-code
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

_INTERESTING_CLAIMS: Final = (
    "aud",
    "iss",
    "tid",
    "oid",
    "sub",
    "azp",
    "appid",
    "roles",
    "scp",
    "idtyp",
    "preferred_username",
    "upn",
    "email",
    "name",
)


def decode_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it. For inspection only."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT (expected three dot-separated parts)")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def print_claims(claims: dict[str, Any]) -> None:
    for key in _INTERESTING_CLAIMS:
        if key in claims:
            print(f"  {key}: {claims[key]}")
    exp = claims.get("exp")
    if isinstance(exp, int):
        remaining = exp - int(time.time())
        print(f"  exp: {exp} (in {remaining // 60} min)")
    present = {k for k in _INTERESTING_CLAIMS if k in claims}
    if not {"upn", "email", "preferred_username"} & present:
        print()
        print("  NOTE: no user identity claim (upn/email/preferred_username).")
        print("  This is an app-only token. It identifies the service principal,")
        print("  not a person. Use a delegated token for per-user identity.")


def call_litellm(base_url: str, token: str, route: str) -> int:
    """Present the Microsoft token as a bearer to a LiteLLM route."""
    url = f"{base_url.rstrip('/')}{route}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            print(f"HTTP {response.status}")
            print(body[:2000].decode(errors="replace"))
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"HTTP {exc.code}")
        print(detail[:2000])
        if exc.code == 403 and "enterprise" in detail.lower():
            print()
            print("The proxy says JWT auth is not licensed. Set general_settings.enable_jwt_auth")
            print("and confirm /health/license reports enterprise.")
        return 1


def device_code_flow(tenant: str, client_id: str, client_secret: str | None) -> int:
    """Get a real delegated USER token. Requires one browser interaction."""
    scope = f"openid profile email {client_id}/.default offline_access"
    data = urllib.parse.urlencode({"client_id": client_id, "scope": scope}).encode()
    with urllib.request.urlopen(
        urllib.request.Request(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
            data=data,
            method="POST",
        ),
        timeout=30,
    ) as response:
        device = json.loads(response.read())

    print("Open this URL and enter the code, then sign in with Microsoft:")
    print(f"  URL : {device['verification_uri']}")
    print(f"  CODE: {device['user_code']}")
    print()
    print("Waiting for you to complete sign-in...")

    interval = int(device.get("interval", 5))
    deadline = time.monotonic() + int(device.get("expires_in", 900))
    while time.monotonic() < deadline:
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device["device_code"],
        }
        if client_secret:
            payload["client_secret"] = client_secret
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                    data=urllib.parse.urlencode(payload).encode(),
                    method="POST",
                ),
                timeout=30,
            ) as response:
                result = json.loads(response.read())
            token = result.get("access_token")
            if token:
                print()
                print("Got a delegated user token. Claims:")
                print_claims(decode_claims(token))
                print()
                print("TOKEN:", token)
                return 0
        except urllib.error.HTTPError as exc:
            error = json.loads(exc.read())
            if error.get("error") != "authorization_pending":
                print(f"token request failed: {error}", file=sys.stderr)
                return 1
        time.sleep(interval)
    print("timed out waiting for the browser step", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="decode a token's claims, no network")
    inspect.add_argument("--token", required=True)

    call = sub.add_parser("call", help="present the token to LiteLLM")
    call.add_argument("--base-url", required=True)
    call.add_argument("--token", required=True)
    call.add_argument("--route", default="/v1/models", help="any data-plane route, default /v1/models")

    device = sub.add_parser("device-code", help="get a real delegated user token")
    device.add_argument("--tenant", required=True)
    device.add_argument("--client-id", required=True)
    device.add_argument("--client-secret", default=None, help="needed for a confidential client")

    args = parser.parse_args(argv[1:])

    if args.command == "inspect":
        print_claims(decode_claims(args.token))
        return 0
    if args.command == "call":
        return call_litellm(args.base_url, args.token, args.route)
    return device_code_flow(args.tenant, args.client_id, args.client_secret)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
