#!/usr/bin/env python3
"""Idempotent DEVELOPER-account rollout: defaults, teams, forced-reset password; never key reassignment."""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(__file__).resolve().parent
KEY_SOURCE = ROOT / "oicm-litellm-layer/scripts/get_master_key.py"
GATEWAY = "https://litellm.ecouncil.ae"
DEVELOPER = "05cc86a3-37e0-4cd2-a25c-51a66c6d61a7"
PILOT = "vietth@ecouncil.ae"
JOURNAL = WORK / "rollout-journal.jsonl"
PASSWORD_FILE = WORK / "shared-initial-password.txt"
PATHPILOT = WORK / "pilot-initial-password.txt"


def admin_key():
    return subprocess.check_output(["python3", str(KEY_SOURCE)], text=True).strip()


def api(token, method, route, data=None):
    request = urllib.request.Request(GATEWAY + route, data=json.dumps(data).encode() if data is not None else None,
                                     method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def require(result, expected=200):
    status, body = result
    if status != expected:
        raise RuntimeError(f"HTTP {status}: {str(body)[:600]}")
    return body


def pages(token, route, kind):
    rows = []
    next_page = 1
    total_pages = 1
    while next_page <= total_pages:
        prefix = route + ("&" if "?" in route else "?")
        size = "size=100" if route.startswith("/key/list") else "page_size=100"
        body = require(api(token, "GET", f"{prefix}page={next_page}&{size}"))
        field = body[kind]
        field = list(field.values()) if isinstance(field, dict) else field
        assert all(isinstance(row, dict) for row in field), f"need full {kind} objects"
        rows.extend(field)
        total_pages = body["total_pages"]
        next_page += 1
    total = body.get("total", body.get("total_count"))
    assert total is None or total == len(rows), f"incomplete {kind} pages"
    return rows


def key_candidates(token):
    keys = pages(token, f"/key/list?team_id={DEVELOPER}&return_full_object=true", "keys")
    identities = {}
    for key in keys:
        alias = key.get("key_alias")
        if not alias or "@" not in alias:
            continue
        normalized = alias.strip().casefold()
        assert normalized not in identities, f"duplicate normalized identity: {normalized}"
        identities[normalized] = key
    assert identities, "no matching candidates"
    return identities


def all_users(token):
    return pages(token, "/user/list", "users")


def matching_user(rows, email):
    matches = [row for row in rows if (row.get("user_email") or "").casefold() == email]
    assert len(matches) <= 1, f"duplicate account email: {email}"
    return matches[0] if matches else None


def settings(token):
    current = require(api(token, "GET", "/get/internal_user_settings"))["values"]
    assert current["user_role"] == "internal_user", current
    assert len(current["teams"]) == 2, "configured default teams changed"
    return current["teams"]


def info(token, user_id):
    return require(api(token, "GET", f"/user/info?user_id={urllib.parse.quote(user_id)}"))


def missing_teams(token, user_id, required):
    for _ in range(20):
        present = {row["team_id"] for row in info(token, user_id)["teams"]}
        missing = {team["team_id"] for team in required} - present
        if not missing:
            return missing
        time.sleep(1)
    return missing


def ensure_membership(token, user_id, email, team_id):
    response = api(token, "POST", "/team/member_add", {
        "team_id": team_id,
        "member": {"user_id": user_id, "user_email": email, "role": "user"},
    })
    if response[0] != 200:
        # Add is a create operation. Simultaneous mutation is a hard stop, not swallowed error.
        raise RuntimeError(f"team attachment {team_id}: {response}")


def journal_read():
    rows = {}
    if JOURNAL.exists():
        for line in JOURNAL.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["email"]] = row
    return rows


def journal_write(row):
    line = json.dumps(row, default=str, sort_keys=True) + "\n"
    with JOURNAL.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    JOURNAL.chmod(0o600)


def assert_complete(token, row, created):
    if not row["user_email"] or row["user_email"].strip().casefold() != row["user_email"].casefold() or row["user_role"] != "internal_user":
        raise RuntimeError(f"Incorrect account identity/default: {row}")
    required = [DEVELOPER, "b19ad7de-997e-45ec-ba34-f7980dccf621", "d1406939-aef6-4205-a8b2-7616c5a72663"]
    missing = missing_teams(token, row["user_id"], [{"team_id": team_id} for team_id in required])
    assert not missing, f"Missing required memberships: {row}"
    for _ in range(20):
        info(token, row["user_id"])
        teams = [require(api(token, "GET", f"/team/info?team_id={team_id}")) for team_id in required]
        complete = all(
            any(member["user_id"] == row["user_id"] and member["role"] == "user" for member in team["team_info"]["members_with_roles"])
            and any(member["user_id"] == row["user_id"] for member in team["team_memberships"])
            for team in teams
        )
        if complete:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"Team memberships did not reconcile after twenty attempts: {row}")
    account = info(token, row["user_id"])
    if created:
        assert account["user_info"]["password_reset_required"] is True


def claim_password():
    source = PATHPILOT
    token_source = source.read_text().strip()
    assert 12 <= len(token_source) <= 40, "pilot handoff source length unexpected"
    if not PASSWORD_FILE.exists():
        PASSWORD_FILE.write_text(token_source + "\n")
    PASSWORD_FILE.chmod(0o600)
    assert stat.S_IMODE(PASSWORD_FILE.stat().st_mode) == 0o600
    password = PASSWORD_FILE.read_text().strip()
    assert 12 <= len(password) <= 40
    return password


def key_fingerprint(key):
    runtime_counters = {"last_active", "spend", "total_spend", "created_at", "updated_at", "updated_by"}
    return {name: value for name, value in key.items() if name not in runtime_counters} | {
        "user_id": key.get("user_id") or "default_user_id"
    }


def user_by_id(token, user_id):
    record = info(token, user_id)["user_info"]
    return {"user_id": user_id, "user_email": record["user_email"], "user_role": record["user_role"]}


def assert_password_state(token, user_id):
    row = info(token, user_id)["user_info"]
    if row["password_reset_required"] is False:
        raise RuntimeError(f"Account {user_id} completed reset, refusing to overwrite it")
    if row["password_reset_required"] not in (None, True):
        raise RuntimeError(f"Unexpected password state type: {row}")


def decode_wrapper(token):
    sections = token.split(".")
    assert len(sections) == 3
    return json.loads(base64.urlsafe_b64decode(sections[1] + "=" * (-len(sections[1]) % 4)))


def verify_pilot_flow(token, user_id, email, password):
    wrapper = require(api(token, "POST", "/v2/login", {"username": email, "password": password}))["token"]
    claims = decode_wrapper(wrapper)
    assert claims["user_id"] == user_id and claims["password_reset_required"] is True
    session = claims["key"]
    try:
        profile = require(api(session, "GET", "/management/v1/users/me"))
        assert profile == {"user_id": user_id, "user_role": "internal_user", "user_email": email}, profile
        status, _ = api(session, "GET", "/key/info")
        assert status == 403, f"password reset key unexpectedly has key-info: {status}"
        status, _ = api(session, "POST", "/user/password/change", {
            "current_password": "intentionally-wrong-current-password", "new_password": "A7OnlyProbe8@",
        })
        assert status == 400, f"wrong current password unexpectedly accepted: {status}"
    finally:
        require(api(session, "POST", "/session/logout"))
    assert info(token, user_id)["user_info"]["password_reset_required"] is True


def rollout(dry_run):
    with (WORK / "rollout.lock").open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = admin_key()
        users = all_users(token)
        candidates = key_candidates(token)
        defaults = settings(token)
        initial_state = {email: {"alias": key["key_alias"], "token": key["token"], "user_id": key["user_id"],
                                 "non_owner_state": key_fingerprint(key)} for email, key in candidates.items()}
        preexisting = {email for email in candidates if matching_user(users, email)}
        complete = journal_read()
        print(json.dumps({"dry_run": dry_run, "selected": len(candidates), "preexisting": len(preexisting),
                          "new": len(candidates) - len(preexisting), "completed": len(complete),
                          "password_changes_existing_users": 0, "key_transfers": 0}))
        if dry_run:
            return initial_state
        for email, original_key in candidates.items():
            current = matching_user(users, email)
            journal = journal_read().get(email)
            if current:
                assert current["user_role"] == "internal_user"
                user_id = current["user_id"]
            else:
                created = require(api(token, "POST", "/user/new", {
                    "user_email": email, "user_role": "internal_user", "auto_create_key": False,
                    "teams": defaults + [{"team_id": DEVELOPER, "user_role": "user", "max_budget_in_team": None}],
                }))
                assert created.get("key") is None
                user_id = created["user_id"]
                current = user_by_id(token, user_id)
                users.append(current)
                journal_write({"email": email, "user_id": user_id, "new_password_required": True,
                               "password_completed": False})
                journal = journal_read()[email]
            known_new_account = journal and journal["new_password_required"]
            first_shared_pilot = email == PILOT and not journal
            if journal and journal.get("password_completed"):
                assert_complete(token, current, False)
                continue
            managed_credential = bool(known_new_account or first_shared_pilot)
            missing = missing_teams(token, user_id, defaults + [{"team_id": DEVELOPER}])
            for team in defaults + [{"team_id": DEVELOPER}]:
                if team["team_id"] in missing:
                    ensure_membership(token, user_id, email, team["team_id"])
            assert not missing_teams(token, user_id, defaults + [{"team_id": DEVELOPER}]), email
            if journal and journal.get("password_completed"):
                assert_complete(token, current, False)
                continue
            reset_flag = info(token, user_id)["user_info"]["password_reset_required"]
            if reset_flag is False:
                assert_complete(token, current, False)
                journal_write({"email": email, "user_id": user_id, "new_password_required": False,
                               "password_completed": False, "three_teams_verified": True,
                               "password_state": "already_reset_by_user_no_overwrite"})
                continue
            if managed_credential:
                endpoint_result = api(token, "POST", "/user/update", {"user_id": user_id, "password": claim_password()})
                require(endpoint_result)
            assert_complete(token, current, managed_credential)
            journal_write({"email": email, "user_id": user_id, "new_password_required": managed_credential,
                           "password_completed": managed_credential,
                           "three_teams_verified": True, "original_key_token": original_key["token"],
                           "initial_key_owner": original_key.get("user_id"),
                           "password_state": "forced_reset" if managed_credential else "unchanged_existing"})


def verify():
    token = admin_key()
    records = journal_read()
    candidates = key_candidates(token)
    complete = {
        email: record for email, record in records.items()
        if record.get("three_teams_verified") and (
            not record.get("new_password_required") or record.get("password_completed")
        )
    }
    assert set(complete) == set(candidates), f"incomplete account rollout: missing={sorted(set(candidates) - set(complete))}"
    snapshot = json.loads((WORK / "initial-key-snapshot.json").read_text())
    assert set(snapshot) == set(candidates), "candidate key inventory changed"
    users = all_users(token)
    flags = []
    for email, record in complete.items():
        user = matching_user(users, email)
        assert user and user["user_id"] == record["user_id"]
        assert_complete(token, user, record["new_password_required"])
        before = snapshot[email]
        after = candidates[email]
        assert key_fingerprint(after) == key_fingerprint(before["non_owner_state"])
        assert (after["user_id"] or "default_user_id") == (before["user_id"] or "default_user_id"), email
        assert len(pages(token, f"/key/list?key_hash={after['token']}&return_full_object=true", "keys")) == 1
        assert after["token"] == before["token"]
        assert after["key_alias"] == before["alias"]
        flags.append({"email": email, "user_id": user["user_id"], "reset": record["new_password_required"],
                      "teams": 3, "ownership_unchanged": True})
    counts = {state: sum(row["reset"] is state for row in flags) for state in (True, False)}
    print(json.dumps({"verified_accounts": len(flags), **counts,
                      "all_key_owners_aliases_entitlements_unchanged": len(flags),
                      "first_browser_route": "inner session sent by frontend to self-service password path"},
                     indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Account-only idempotent rollout. No argument changes key ownership")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        before = rollout(True)
        snapshot = WORK / "initial-key-snapshot.json"
        if not snapshot.exists():
            snapshot.write_text(json.dumps(before, indent=2))
        snapshot.chmod(0o600)
        print("Snapshot captured with original ownership; no production changes")
    elif args.verify:
        verify()
    else:
        rollout(False)
        verify()
