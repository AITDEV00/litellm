#!/usr/bin/env python3
# ruff: noqa: T201  -- a CLI exemplar; printing the flow to the operator is the point
"""Flow A: CLI device-code SSO -> short-lived data-plane token, programmatic.

This is the non-enterprise path for turning a Microsoft SSO login into a token
the data plane accepts. It is the protocol the `litellm` CLI speaks, reproduced
here so a service can drive it without shelling out.

Protocol, verified against litellm/proxy/management_endpoints/ui_sso.py:

  1. POST /sso/cli/start
       -> {"login_id", "poll_secret", "user_code", "verification_uri_complete"}
  2. A HUMAN opens verification_uri_complete and signs in with Microsoft.
     This step is the point of the flow; it cannot be automated. The design
     assumes a real person at a real browser.
  3. GET /sso/cli/poll/{login_id}  header: x-litellm-cli-poll-secret
       -> {"status": "pending"}                     (browser step not done)
       -> {"status": "ready", "key": <token>, ...}  (single team, or team chosen)
       -> {"status": "ready", "requires_team_selection": true, "teams": [...]}
           (multi-team user; re-poll with ?team_id=<id>)
  4. The flow is single-use: the server deletes the session when it returns
     the token.

The `key` returned is a JWT (a CLI session token), NOT an `sk-` virtual key. It
is the intended data-plane credential and it expires. Send it as
`Authorization: Bearer` exactly like any other key.

Because a human must complete step 2, this fits account linking and interactive
setup. For unattended use with the user's own Microsoft token, use Flow B
(enable_jwt_auth) instead.

Usage:
    python3 flow_a_cli_sso_virtual_key.py --base-url http://localhost:14001
    python3 flow_a_cli_sso_virtual_key.py --base-url http://localhost:14001 --wait-seconds 300
    python3 flow_a_cli_sso_virtual_key.py --base-url http://localhost:14001 --team-id <team>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

_POLL_HEADER: Final = "x-litellm-cli-poll-secret"
_FATAL_POLL_STATUSES: Final = frozenset({401, 403, 404, 410})


def _post_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def _get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def start_flow(base_url: str) -> dict[str, Any]:
    """Step 1: create the login flow and get the URL a human must open."""
    return _post_json(f"{base_url}/sso/cli/start")


def poll(base_url: str, login_id: str, poll_secret: str, team_id: str | None = None) -> tuple[int, Any]:
    """Step 3: one poll. Optionally with ?team_id= for a multi-team user."""
    url = f"{base_url}/sso/cli/poll/{login_id}"
    if team_id:
        url = f"{url}?{urllib.parse.urlencode({'team_id': team_id})}"
    return _get_json(url, headers={_POLL_HEADER: poll_secret})


def poll_until_ready(
    base_url: str, login_id: str, poll_secret: str, wait_seconds: int, team_id: str | None
) -> dict[str, Any] | None:
    """Poll until a token is returned, the wait budget runs out, or the poll is rejected."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status, body = poll(base_url, login_id, poll_secret, team_id)
        if status in _FATAL_POLL_STATUSES:
            print(f"  poll rejected with HTTP {status}", file=sys.stderr)
            return None
        if isinstance(body, dict):
            if body.get("key"):
                return body
            if body.get("requires_team_selection") and team_id is None:
                print(f"  multi-team user, choose one of: {body.get('teams')}")
                print("  re-run with --team-id <id>")
                return None
        time.sleep(2)
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:14001 or https://litellm.ecouncil.ae")
    parser.add_argument("--wait-seconds", type=int, default=10, help="how long to poll for the human step")
    parser.add_argument("--team-id", default=None, help="team to mint the token for, for multi-team users")
    args = parser.parse_args(argv[1:])

    base_url = args.base_url.rstrip("/")

    print("Step 1: POST /sso/cli/start")
    started = start_flow(base_url)
    login_id = started["login_id"]
    poll_secret = started["poll_secret"]
    print(f"  login_id  : {login_id}")
    print(f"  user_code : {started.get('user_code')}")
    print()
    print("Step 2 (HUMAN): open this URL and sign in with Microsoft")
    print(f"  {started.get('verification_uri_complete') or base_url + '/sso/key/generate'}")
    print()

    print(f"Step 3: polling /sso/cli/poll/{login_id} for {args.wait_seconds}s")
    result = poll_until_ready(base_url, login_id, poll_secret, args.wait_seconds, args.team_id)
    if result is None:
        print("  no token yet. If nobody opened the URL, that is expected.")
        print("  Re-run with --wait-seconds 300 and open the URL in a browser.")
        return 0

    print()
    print("Done. The SSO identity is now a data-plane token.")
    print(f"  user_id : {result.get('user_id')}")
    print(f"  team_id : {result.get('team_id')}")
    print(f"  teams   : {result.get('teams')}")
    token = result.get("key", "")
    print(f"  token   : {token[:16]}...{token[-6:]}  (truncated, this is a JWT)")
    print()
    print("Use it on the data plane exactly like a virtual key:")
    print(f'  curl -s {base_url}/v1/models -H "Authorization: Bearer <token>"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
