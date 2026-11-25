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
COMMITTED_OWNERS = WORK / "key-transfer-journal.jsonl"
RAW_OWNER_EVIDENCE = WORK / "key-transfer-raw-owner-evidence.json"


def committed_targets():
    records = {}
    if COMMITTED_OWNERS.exists():
        for row in (json.loads(line) for line in COMMITTED_OWNERS.read_text().splitlines() if line.strip()):
            if row.get("owner_verified") is True:
                records[row["email"]] = row["new_owner"]
    return records


def preflight(admin_token=None, raw_owners=None, pending_requests=None):
    token = admin_token or admin_key()
    baseline = json.loads(BASELINE.read_text())
    candidates = key_candidates(token)
    users = all_users(token)
    assert stat_mode(ORIGINAL_SNAPSHOT) == 0o600, "The pre-transfer snapshot must be protected"
    assert stat_mode(BASELINE) == 0o600, "Transfer baseline must be mode 0600"
    assert set(candidates) == set(baseline), "Key inventory differs from original baseline"
    assert len({key["token"] for key in candidates.values()}) == len(candidates)
    committed = committed_targets()
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
        current = candidates[email]
        if email in committed:
            assert current.get("user_id") == target_id, f"Committed-owner mismatch: {email}"
        original["user_id"] = current.get("user_id")
        assert key_fingerprint(original) == key_fingerprint(current), f"Key config drift: {email}"
        assert committed.get(email, target_id) == target_id, f"Journal target mismatch: {email}"
        assert current["team_id"] == DEVELOPER
        if raw_owners is not None:
            for worker, records in raw_owners.items():
                actual = records.get(before["token"])
                assert actual is not None, f"Missing {email} on {worker}"
                expected_owner = (
                    target_id if email in committed or email in (pending_requests or {}) else before["original_user_id"]
                )
                assert actual["user_id"] == expected_owner, f"Raw owner mismatch for {email} on {worker}"
                assert actual["key_alias"] == before["alias"], f"Raw alias mismatch for {email} on {worker}"
        rows.append(
            {
                "email": email,
                "original_user_id": current.get("user_id"),
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
                "confirmed_personal_owners": len(committed),
                "remaining_original_proxy_admin": sum(
                    row["original_user_id"] == "default_user_id" and row["email"] not in committed for row in rows
                ),
                "remaining_original_null": sum(
                    row["original_user_id"] is None and row["email"] not in committed for row in rows
                ),
                "target_accounts": len({row["planned_update"]["user_id"] for row in rows}),
                "shared_service_key_excluded": True,
            },
            indent=2,
        )
    )
    return baseline, candidates, users, committed


def verify(selected_emails=None):
    token = admin_key()
    baseline = json.loads(BASELINE.read_text())
    candidates = key_candidates(token)
    users = all_users(token)
    assert set(candidates) == set(baseline)
    selected_emails = sorted(baseline) if selected_emails is None else sorted(selected_emails)
    assert set(selected_emails) <= set(baseline), "Selected key is outside the original migration scope"
    selected = [(email, baseline[email]) for email in selected_emails]
    assert len(selected) == len(selected_emails)
    flags = []
    for email, before in selected:
        key = candidates[email]
        account = matching_user(users, email)
        assert account and key["user_id"] == account["user_id"], f"Owner mismatch: {email}"
        assert key["token"] == before["token"] and key["key_alias"] == before["alias"]
        updated = dict(before["original_key"])
        updated["user_id"] = account["user_id"]
        updated["settings_updated_at"] = key["settings_updated_at"]
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
    filename = "key-transfer-audit.json" if selected_emails == sorted(baseline) else "selected-key-transfer-audit.json"
    report = WORK / filename
    summary = {
        "verified_personal_owners": len(flags),
        "distinct_personal_users": len({row["user_id"] for row in flags}),
        "remaining_original_owners": len(baseline) - len(flags),
        "complete_transfer": len(flags) == len(baseline),
        "selected_emails": selected_emails,
        "key_team": DEVELOPER,
        "key_configuration_preserved": True,
        "shared_service_key_not_assigned_to_migrated_person": True,
    }
    assert RAW_OWNER_EVIDENCE.exists(), "Run the documented raw Prisma owner audit first"
    assert stat_mode(RAW_OWNER_EVIDENCE) == 0o600, "Raw evidence must remain protected"
    raw_records = json.loads(RAW_OWNER_EVIDENCE.read_text())
    for email, before in selected:
        account = matching_user(users, email)
        assert raw_records[email] == account["user_id"], f"Raw owner mismatch: {email}"
    summary["raw_prisma_owner_matches"] = len(flags)
    if selected_emails == sorted(baseline):
        assert set(raw_records) == set(candidates), "Full acceptance requires raw evidence for every original target"
        assert set(committed_targets()) == set(baseline), "Full acceptance requires all targets committed"
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
    parser.add_argument("--pilot", action="store_true", help="Read-only single-pilot acceptance")
    parser.add_argument("--selected", action="store_true", help="Read-only acceptance for committed transfer targets")
    arguments = parser.parse_args()
    if arguments.pilot:
        verify({"vietth@ecouncil.ae"})
    elif arguments.selected:
        verify(set(committed_targets()))
    elif arguments.verify:
        verify()
    else:
        preflight()
