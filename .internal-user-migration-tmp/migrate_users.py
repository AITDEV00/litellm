#!/usr/bin/env python3
"""Guarded internal-user migration. Secret files stay local; remove directory after handoff."""
from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import sys
import urllib.error
import string
import stat
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(__file__).resolve().parent
ADMIN_SCRIPT = ROOT / "oicm-litellm-layer/scripts/get_master_key.py"
BASE = "https://litellm.ecouncil.ae"
DEVELOPER = "05cc86a3-37e0-4cd2-a25c-51a66c6d61a7"
PILOT = "vietth@ecouncil.ae"


def admin_key():
    return subprocess.check_output(["python3", str(ADMIN_SCRIPT)], text=True).strip()


def endpoint(token, method, route, data=None):
    request = urllib.request.Request(BASE + route, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {route}: HTTP {e.code}: {e.read().decode()[:800]}") from None


def listing(token, route, kind):
    rows = []
    total_pages = 1
    page = 1
    while page <= total_pages:
        separator = "&" if "?" in route else "?"
        size = "size=100" if route.startswith("/key/list") else "page_size=100"
        result = endpoint(token, "GET", f"{route}{separator}page={page}&{size}")
        field = result[kind]
        if isinstance(field, dict):
            field = list(field.values())
        assert all(isinstance(row, dict) for row in field), f"{kind} are not full objects"
        rows.extend(field)
        total_pages = result["total_pages"]
        page += 1
    count = result.get("total", result.get("total_count"))
    assert count is None or count == len(rows), f"partial {kind} response"
    return rows


def candidate_keys(token):
    keys = listing(token, f"/key/list?team_id={DEVELOPER}&return_full_object=true", "keys")
    return {
        row["key_alias"].strip().casefold(): row for row in keys
        if row.get("key_alias") and "@" in row["key_alias"]
    }


def find_user(token, email):
    users = listing(token, "/user/list?user_email=" + urllib.parse.quote(email), "users")
    matches = [row for row in users if row.get("user_email", "").casefold() == email]
    assert len(matches) <= 1, f"multiple identity matches for {email}"
    return matches[0] if matches else None


def teams(token):
    values = endpoint(token, "GET", "/get/internal_user_settings")["values"]
    assert values["user_role"] == "internal_user", values
    assert len(values["teams"]) == 2, "unexpected configured team default change"
    return values["teams"] + [{"team_id": DEVELOPER, "user_role": "user"}]


def snapshot(token):
    candidates = candidate_keys(token)
    assert len(candidates) == 53, "candidate count changed; review additions/deletions"
    users = [find_user(token, email) for email in candidates]
    assert all(user["user_id"] for user in users if user)
    return {"candidates": candidates, "users": users, "required_teams": teams(token)}


def member_required(token, user_id):
    response = endpoint(token, "GET", f"/user/info?user_id={urllib.parse.quote(user_id)}")
    assert not response["keys"] or response["user_id"] != user_id
    actual = {team["team_id"] for team in response["teams"]}
    required = {team["team_id"] for team in teams(token)}
    assert required <= actual, {"missing_teams": sorted(required - actual)}
    response = endpoint(token, "GET", f"/team/info?team_id={DEVELOPER}")
    matching = [row for row in response["team_info"]["members_with_roles"] if row["user_id"] == user_id]
    assert len(matching) == 1 and matching[0]["role"] == "user"


def ensure_user(token, email):
    user = find_user(token, email)
    if user:
        assert user["user_role"] == "internal_user", f"existing role must be protected: {email}"
        user_id = user["user_id"]
    else:
        assert email == (WORK / "initial-user-email.txt").read_text().strip(), "resume only the uncompleted already-known account"
        created = endpoint(token, "POST", "/user/new", {
            "user_email": email, "user_role": "internal_user",
            "auto_create_key": False, "teams": teams(token),
        })
        user_id = created["user_id"]
        assert not created.get("key"), "must not create duplicate LLM keys"
    info = endpoint(token, "GET", f"/user/info?user_id={urllib.parse.quote(user_id)}")
    assert not info["keys"], "no new account should start with unexpected keys"
    actual = {row["team_id"] for row in info["teams"]}
    for team in teams(token):
        if team["team_id"] not in actual:
            endpoint(token, "POST", "/team/member_add", {
                "team_id": team["team_id"], "member": {
                    "user_id": user_id, "user_email": email, "role": team.get("user_role", "user")}})
    member_required(token, user_id)
    return next(row for row in endpoint(token, "GET", f"/user/list?user_email={urllib.parse.quote(email)}&page=1&page_size=100")["users"]
                if row["user_email"].casefold() == email)


def query_db(sql):
    code = 'import asyncio,sys;from prisma import Prisma\nasync def main():\n db=Prisma();await db.connect();print(await db.query_first(sys.stdin.read()));await db.disconnect()\nasyncio.run(main())'
    result = subprocess.run(["kubectl", "-n", "adeo-litellm", "exec", "-i", "deploy/litellm-proxy",
                             "--", "python", "-c", code], input=sql, text=True, capture_output=True,
                            timeout=65, check=True)
    return result.stdout


def verify_reset(email, user_id):
    # Identity is UUID/email derived from the trusted gateway API, not freeform text.
    assert len(user_id) == 36 and all(c in "0123456789abcdef-" for c in user_id)
    assert email == email.strip().lower() and email.endswith("@ecouncil.ae")
    sql = ('SELECT CASE WHEN password IS NOT NULL AND password_reset_required IS TRUE AND '
           f"user_role = 'internal_user' AND user_email = '{email}' THEN 'true' ELSE 'false' END "
           f'AS reset_required_ok FROM "LiteLLM_UserTable" WHERE user_id = \'{user_id}\';')
    assert "'true'" in query_db(sql), query_db(sql)
    return True


def password_write(token, email, password_file):
    user = find_user(token, email)
    assert user is None, "password writes are only for newly created accounts"
    password_path = WORK / "initial-password.txt"
    if password_file.exists():
        raise RuntimeError("password file exists; don't reset existing account on rerun")
    if password_path.exists() and password_path != password_file:
        raise RuntimeError("existing hands-off initial password means this account needs manual review")
    password_path.write_text("")
    password_path.chmod(0o600)
    (WORK / "initial-user-email.txt").write_text(email + "\n")
    (WORK / "initial-user-email.txt").chmod(0o600)
    user_id = ensure_user(token, email)["user_id"]
    if password_file.exists():
        raise RuntimeError("user creation succeeded before password, resume with its saved initial password")
    password_path.unlink()
    characters = string.ascii_letters + string.digits + "@#$%&*?"
    while True:
        candidate = "A7" + "".join(secrets.choice(characters) for _ in range(10)) + "@"
        if any(c.islower() for c in candidate) and any(c.isdigit() for c in candidate) and any(c in "@#$%&*?" for c in candidate):
            break
    endpoint(token, "POST", "/user/update", {"user_id": user_id, "password": candidate})
    password_file.write_text(candidate + "\n")
    password_file.chmod(0o600)
    verify_reset(email, user_id)
    return user_id


def jwt_claims(token):
    parts = token.split(".")
    assert len(parts) == 3
    return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))


def parse_error(message):
    marker = ": HTTP "
    if marker not in message:
        raise ValueError(message)
    return int(message.split(marker, 1)[1].split(":", 1)[0])

# Migration CLI superset: --pilot is only allowed accounting/verification action.


def verify_password_session(email, user_id, password):
    login = endpoint(admin_key(), "POST", "/v2/login", {"username": email, "password": password})
    outer = login["token"]
    claims = jwt_claims(outer)
    assert claims["password_reset_required"] is True and claims["user_id"] == user_id
    assert claims["login_method"] == "username_password"
    session = claims["key"]
    info_endpoint = f"/v2/user/info?user_id={user_id}"
    restricted = {}
    try:
        try:
            endpoint(session, "GET", info_endpoint)
            raise RuntimeError("password-reset session must deny general info")
        except RuntimeError as e:
            assert parse_error(str(e)) == 403, str(e)
            restricted["restricted_info"] = 403
        try:
            endpoint(session, "POST", "/user/password/change", {"current_password": "wrong-password", "new_password": "A7OnlyProbe8@"})
            raise RuntimeError("wrong current password accepted")
        except RuntimeError as e:
            assert parse_error(str(e)) == 400, str(e)
            restricted["incorrect_password_denied"] = 400
    finally:
        logout = endpoint(session, "POST", "/session/logout")
    try:
        endpoint(outer, "GET", info_endpoint)
        outer_status = 200
    except RuntimeError as e:
        outer_status = parse_error(str(e))
    restricted["wrapped_session_error_status"] = outer_status
    restricted["wrapped_session_user_reset_flag"] = True
    restricted["underlying_session_logout_response"] = logout.get("message")
    return restricted


def preserve_key(row, before):
    excluded = {"created_at", "updated_at", "updated_by"}
    assert project_key(row) == project_key(before), {"key_mutation": row}
    return True


def project_key(row):
    return {key: value for key, value in row.items() if key not in {"user_id", "created_at", "updated_at", "updated_by"}}


def provision(args):
    token = admin_key()
    state = snapshot(token)
    for email, user in zip(state["candidates"], state["users"]):
        if args.pilot and email != PILOT:
            continue
        if user and not args.pilot:
            ensure_user(token, email)  # no password update for existing users
            continue
        if user and args.pilot:
            member_required(token, user["user_id"])
            password = (WORK / "pilot-initial-password.txt").read_text().strip()
            forced_reset = verify_reset(email, user["user_id"])
            session_result = verify_password_session(email, user["user_id"], password)
            key = json.loads((WORK / "pilot-before-key.json").read_text())
            current_key = candidate_keys(token)[email]
            assert current_key["user_id"] == key["former_user_id"]
            assert project_key(current_key) == key["key_state"]
            display = {"email": email, "user_id": user["user_id"], "forced_reset": forced_reset,
                       "all_three_teams": "verified", "existing_key_preserved": True,
                       "password_handoff": "pilot-initial-password.txt (mode 0600)",
                       "reset_session": session_result}
            print(json.dumps(display, indent=2))
            (WORK / "pilot-result.json").write_text(json.dumps(display, indent=2))
            (WORK / "pilot-result.json").chmod(0o600)
            continue
        if not (args.pilot or args.rollout):
            raise RuntimeError("account creation requires --pilot or explicitly approved --rollout")
        if user is None:
            path = WORK / "initial-password.txt"
            user_id = password_write(token, email, path)
            password = path.read_text().strip()
            (WORK / "initial-user-email.txt").write_text(email + "\n")
            (WORK / "initial-user-id.txt").write_text(user_id + "\n")
            info = verify_password_session(email, user_id, password)
            print(json.dumps({"email": email, "user_id": user_id, "role": "internal_user", "forced_reset": True,
                              "membership": "verified all three", "session": info}, indent=2))


def migrate_keys(single):
    token = admin_key()
    state = snapshot(token)
    assert all(user for user in state["users"]), "every candidate must have an explicit identity"
    for email, before, user in zip(state["candidates"], state["candidates"].values(), state["users"]):
        if single and email != PILOT:
            continue
        if before["user_id"] == user["user_id"]:
            continue
        if single and before["user_id"] != "default_user_id":
            raise RuntimeError("review unowned/already-owned key first")
        member_required(token, user["user_id"])
        endpoint(token, "POST", "/key/update", {"key": before["token"], "user_id": user["user_id"]})
        new_keys = candidate_keys(token)
        assert len(new_keys) == len(state["candidates"])
        preserve_key(new_keys[email], before)
        assert new_keys[email]["user_id"] == user["user_id"]
        updated = {key: new_keys[email].get(key) for key in ("user_id", "key_alias", "team_id")}
        print(json.dumps({"email": email, "new": updated}, indent=2))


parser = argparse.ArgumentParser(description="Guarded account and ownership migration, with no key transfer by default")
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--pilot", action="store_true")
mode.add_argument("--rollout", action="store_true")
mode.add_argument("--migrate-pilot-key", action="store_true")
mode.add_argument("--migrate-all-keys", action="store_true")
args = parser.parse_args()
try:
    if args.migrate_pilot_key:
        raise RuntimeError("Key transfer awaits pilot auth remediation and separately requested approval")
    elif args.migrate_all_keys:
        raise RuntimeError("All-key transfer requires separately requested approval, after model interoperability testing")
    elif args.rollout:
        raise RuntimeError("Rollout is paused until users can complete browser self-service password reset; pilot blocker in PLAN.md")
    else:
        provision(args)
except (AssertionError, RuntimeError, KeyError, ValueError) as e:
    print(f"FAILED: {e}", file=sys.stderr)
    sys.exit(1)
