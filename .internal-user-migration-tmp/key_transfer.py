#!/usr/bin/env python3
"""Preflight and verify the ownership transfer without mutating keys."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rollout_accounts import (
    DEVELOPER,
    WORK,
    admin_key,
    all_users,
    assert_complete,
    info,
    key_candidates,
    key_fingerprint,
    matching_user,
    pages,
)

BASELINE = WORK / "key-transfer-baseline.json"
ORIGINAL_SNAPSHOT = WORK / "initial-key-snapshot.json"
RAW_OWNER_EVIDENCE = WORK / "key-transfer-raw-owner-evidence.json"


def preflight():
    token = admin_key()
    baseline = json.loads(BASELINE.read_text())
    candidates = key_candidates(token)
    users = all_users(token)
    assert set(candidates) == set(baseline), "Key inventory differs from original baseline"
    assert len({key["token"] for key in candidates.values()}) == len(candidates)
    rows = []
    for email in sorted(baseline):
        before = baseline[email]
        assert candidates[email]["token"] == before["token"], email
        assert candidates[email]["key_alias"] == before["alias"], email
        user = matching_user(users, email)
        assert user and user["user_role"] == "internal_user"
        assert user["user_email"].strip().casefold() == email
        assert user["user_id"] == before["target_user_id"], f"User identity changed: {email}"
        assert_complete(token, user, False)
        target_id = user["user_id"]
        owned = pages(token, f"/key/list?user_id={target_id}&return_full_object=true", "keys")
        unexpected = [key for key in owned if key["token"] != before["token"]]
        assert not unexpected, f"Unexpected existing target key at {email}"
        original = dict(before["original_key"])
        original["user_id"] = before["original_user_id"]
        current = candidates[email]
        assert key_fingerprint(original) == key_fingerprint(current), f"Owner or key config drift: {email}"
        assert current["team_id"] == DEVELOPER
        rows.append(
            {
                "email": email,
                "original_user_id": original["user_id"],
                "planned_update": {"key": before["token"], "user_id": target_id},
                "key_config_unchanged": True,
            }
        )
    assert stat_mode(BASELINE) == 0o600, "Transfer baseline must be mode 0600"
    assert stat_mode(ORIGINAL_SNAPSHOT) == 0o600, "Original snapshot must be mode 0600"
    print(
        json.dumps(
            {
                "mode": "read-only preflight; no ownership mutations",
                "targets": len(rows),
                "original_proxy_admin": sum(row["original_user_id"] == "default_user_id" for row in rows),
                "original_null": sum(row["original_user_id"] is None for row in rows),
                "target_accounts": len({row["planned_update"]["user_id"] for row in rows}),
                "shared_service_key_excluded": True,
            },
            indent=2,
        )
    )


def verify():
    token = admin_key()
    baseline = json.loads(BASELINE.read_text())
    candidates = key_candidates(token)
    users = all_users(token)
    assert set(candidates) == set(baseline)
    flags = []
    for email, before in baseline.items():
        key = candidates[email]
        account = matching_user(users, email)
        assert account and key["user_id"] == account["user_id"], f"Owner mismatch: {email}"
        assert key["token"] == before["token"] and key["key_alias"] == before["alias"]
        updated = dict(before["original_key"])
        updated["user_id"] = account["user_id"]
        assert key_fingerprint(key) == key_fingerprint(updated), f"Config drift: {email}"
        assert key["team_id"] == DEVELOPER
        assert_complete(token, account, False)
        target_info = info(token, account["user_id"])
        assert target_info["user_info"]["user_id"] == account["user_id"]
        owned = pages(token, f"/key/list?user_id={account['user_id']}&return_full_object=true", "keys")
        owned_hashes = [row["token"] for row in owned]
        assert owned_hashes == [before["token"]], f"Incorrect key set for {email}"
        flags.append({"email": email, "user_id": account["user_id"], "hash": before["token"]})
    all_team_keys = pages(token, f"/key/list?team_id={DEVELOPER}&return_full_object=true", "keys")
    service = [key for key in all_team_keys if key.get("key_alias") == "V2V-DEV-SHARED"]
    assert len(service) == 1, "Shared service key missing"
    assert service[0]["user_id"] not in {row["user_id"] for row in flags}
    report = WORK / "key-transfer-audit.json"
    summary = {
        "verified_personal_owners": len(flags),
        "original_proxy_admin_owners_remaining": 0,
        "distinct_personal_users": len({row["user_id"] for row in flags}),
        "key_team": DEVELOPER,
        "key_configuration_preserved": True,
        "shared_service_key_unassigned": service[0]["user_id"] is None,
    }
    assert RAW_OWNER_EVIDENCE.exists(), "Run the documented raw Prisma owner audit first"
    assert stat_mode(RAW_OWNER_EVIDENCE) == 0o600, "Raw evidence must remain protected"
    raw_records = json.loads(RAW_OWNER_EVIDENCE.read_text())
    assert len(raw_records) == len(flags), "Raw owner evidence must cover all 53"
    for email, before in baseline.items():
        account = matching_user(users, email)
        assert raw_records[email] == account["user_id"], f"Raw owner mismatch: {email}"
    summary["raw_prisma_owner_matches"] = len(flags)
    supported_modes = (None, 0o600)
    assert stat_mode(report) in supported_modes, "Audit file must remain protected"
    report.write_text(json.dumps(summary, indent=2) + "\n")
    report.chmod(0o600)
    print(json.dumps(summary, indent=2))


def stat_mode(path: Path):
    return path.stat().st_mode & 0o777 if path.exists() else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Read-only final audit after an authorized transfer",
    )
    arguments = parser.parse_args()
    if arguments.verify:
        verify()
    else:
        preflight()
