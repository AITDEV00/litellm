#!/usr/bin/env python3
"""Serially transfer original DEVELOPER key ownership after explicit authorization."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
from pathlib import Path

from key_transfer import committed_targets, preflight, verify
from rollout_accounts import (
    DEVELOPER,
    WORK,
    admin_key,
    all_users,
    api,
    assert_complete,
    key_candidates,
    key_fingerprint,
    matching_user,
    pages,
    require,
)

STATE = WORK / "key-transfer-state.jsonl"
LOCK = WORK / "key-transfer.lock"
BASELINE = WORK / "key-transfer-baseline.json"
RAW_EVIDENCE = WORK / "key-transfer-raw-owner-evidence.json"
JOURNAL = WORK / "key-transfer-journal.jsonl"


class ApiClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def transfer(self, digest: str, user_id: str) -> None:
        body = require(api(self.token, "POST", "/key/update", {"key": digest, "user_id": user_id}))
        assert body["token"] == digest, "Written-key hash mismatch"
        assert body["user_id"] == user_id, "Written-owner mismatch"


def stat_mode(path: Path) -> int | None:
    return path.stat().st_mode & 0o777 if path.exists() else None


def append_jsonl(path: Path, row: dict) -> None:
    assert stat_mode(path) in (None, 0o600), f"Sensitive journal must be protected: {path}"
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    path.chmod(0o600)


def save_private_json(path: Path, value: dict) -> None:
    assert stat_mode(path) in (None, 0o600), f"Raw owner evidence is not protected: {path}"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def raw_owners(hashes: list[str]) -> dict[str, dict]:
    pods = production_pods()
    assert len(pods) == 2, f"Expected two production workers, found {len(pods)}"
    return {"worker-a": read_raw_owners(pods[0], hashes), "worker-b": read_raw_owners(pods[1], hashes)}


def read_raw_owners(pod: str, hashes: list[str]) -> dict[str, dict]:
    script = (
        "import asyncio,json;from prisma import Prisma\n"
        "async def main():\n"
        " db=Prisma();await db.connect()\n"
        " try:\n"
        "  rows=await db.litellm_verificationtoken.find_many(where={'token':{'in':%r}})\n"
        "  print('OWNER_JSON:'+json.dumps("
        "[{'token':r.token,'user_id':r.user_id,'key_alias':r.key_alias} for r in rows]))\n"
        " finally:\n"
        "  await db.disconnect()\n"
        "asyncio.run(main())" % hashes
    )
    result = subprocess.run(
        ["kubectl", "-n", "adeo-litellm", "exec", "-i", pod, "--", "python", "-c", script],
        text=True,
        capture_output=True,
        timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"Raw owner read failed on {pod}: {result.stderr[-800:]}")
    lines = [line for line in result.stdout.splitlines() if line.startswith("OWNER_JSON:")]
    if len(lines) != 1:
        raise RuntimeError(f"No structured raw read on {pod}: {result.stdout[-800:]}")
    rows = json.loads(lines[0][len("OWNER_JSON:") :])
    return {row["token"]: row for row in rows}


def plan(baseline: dict, committed: dict) -> list[dict]:
    return [
        {
            "email": email,
            "token": baseline[email]["token"],
            "target_user_id": baseline[email]["target_user_id"],
            "original_user_id": baseline[email]["original_user_id"],
        }
        for email in sorted(baseline, key=lambda email: (baseline[email]["original_user_id"] is None, email))
        if email not in committed
    ]


def reconcile_one(
    email,
    before,
    client,
    get_current_key,
    get_user,
    validate_user,
    get_attached_keys,
    get_raw_owners,
    *,
    owner_evidence_path,
    state_path,
    journal_path,
):
    user = get_user(email)
    assert user and user["user_id"] == before["target_user_id"], f"Incorrect target user: {email}"
    assert committed_targets().get(email, user["user_id"]) == user["user_id"], f"Journal conflict: {email}"
    validate_user(user, email)
    current = get_current_key()
    assert current["token"] == before["token"] and current["key_alias"] == before["alias"]
    assert current["team_id"] == DEVELOPER
    expected = dict(before["original_key"]) | {"user_id": user["user_id"]}
    if current.get("user_id") != user["user_id"]:
        before_raw = get_raw_owners([before["token"]])
        for worker, records in before_raw.items():
            raw = records.get(before["token"])
            assert raw is not None, f"Missing original key on {worker}: {email}"
            assert raw["user_id"] == before["original_user_id"], f"raw owner mismatch before transfer: {email}"
            assert raw["key_alias"] == before["alias"], f"raw alias mismatch before transfer: {email}"
        compare = dict(before["original_key"]) | {"user_id": current.get("user_id")}
        assert key_fingerprint(current) == key_fingerprint(compare), f"configuration drift before transfer: {email}"
        append_jsonl(
            state_path,
            {
                "email": email,
                "status": "request_authorized",
                "original_owner": before["original_user_id"],
                "new_owner": user["user_id"],
                "token": before["token"],
            },
        )
        try:
            client.transfer(before["token"], user["user_id"])
        except Exception:
            append_jsonl(
                state_path,
                {
                    "email": email,
                    "status": "request_outcome_unknown",
                    "original_owner": before["original_user_id"],
                    "new_owner": user["user_id"],
                    "token": before["token"],
                },
            )
            raise
        current = get_current_key()
    assert current.get("user_id") == user["user_id"], f"owner read-back mismatch: {email}"
    expected["settings_updated_at"] = current.get("settings_updated_at")
    assert key_fingerprint(current) == key_fingerprint(expected), f"configuration mismatch: {email}"
    owned = get_attached_keys(user["user_id"])
    assert [entry["token"] for entry in owned] == [before["token"]], f"Incorrect attached key set: {email}"
    validate_user(user, email)
    raw_observations = get_raw_owners([before["token"]])
    for worker, records in raw_observations.items():
        raw = records.get(before["token"])
        assert raw is not None, f"Missing original key on {worker}: {email}"
        assert raw["user_id"] == user["user_id"], f"raw owner mismatch on {worker}: {email}"
        assert raw["key_alias"] == before["alias"], f"raw alias mismatch on {worker}: {email}"
    new_owner = user["user_id"]
    append_jsonl(
        state_path,
        {
            "email": email,
            "status": "raw_verified",
            "original_owner": before["original_user_id"],
            "new_owner": new_owner,
            "token": before["token"],
        },
    )
    save_private_json(
        owner_evidence_path,
        {
            **(json.loads(owner_evidence_path.read_text()) if owner_evidence_path.exists() else {}),
            email: new_owner,
        },
    )
    append_jsonl(
        journal_path,
        {
            "email": email,
            "status": "transfer_verified",
            "http_status": 200,
            "owner_verified": True,
            "original_owner": before["original_user_id"],
            "new_owner": new_owner,
            "token": before["token"],
            "config_preserved": True,
            "raw_owner_verified": True,
        },
    )
    return "transfer_verified"


def production_pods() -> list[str]:
    result = subprocess.check_output(["kubectl", "-n", "adeo-litellm", "get", "pods", "-o", "json"], text=True)
    data = json.loads(result)
    return sorted(
        pod["metadata"]["name"]
        for pod in data["items"]
        if pod["metadata"]["name"].startswith("litellm-proxy-")
        and not pod["metadata"]["name"].startswith("litellm-proxy-dev-")
        and pod["status"].get("phase") == "Running"
    )


def pending_requests(baseline):
    records = {}
    if STATE.exists():
        for row in (json.loads(line) for line in STATE.read_text().splitlines() if line.strip()):
            records[row["email"]] = row
    committed = committed_targets()
    pending = {}
    for email, record in records.items():
        if record["status"] not in ["request_authorized", "request_outcome_unknown", "raw_verified"]:
            continue
        if email in committed:
            continue
        assert email in baseline
        before = baseline[email]
        assert record["token"] == before["token"]
        assert record["new_owner"] == before["target_user_id"]
        assert record["original_owner"] == before["original_user_id"]
        pending[email] = record
    return pending


def run_preflight():
    token = admin_key()
    baseline = json.loads(BASELINE.read_text())
    return preflight(token, raw_owners([entry["token"] for entry in baseline.values()]), pending_requests(baseline))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="Offline manifest listing, including no production reads")
    parser.add_argument(
        "--preflight-raw", action="store_true", help="Full read-only API and two-worker raw owner check"
    )
    parser.add_argument("--acceptance-full-raw", action="store_true", help="Read-only final 53-owner verification")
    parser.add_argument("--apply-remaining", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--max-transfers", type=int, default=1)
    args = parser.parse_args()
    baseline = json.loads(BASELINE.read_text())
    pending = plan(baseline, committed_targets())
    if args.acceptance_full_raw:
        baseline, _, _, committed = run_preflight()
        assert not pending
        assert len(committed) == len(baseline) == 53
        raw = raw_owners([record["token"] for record in baseline.values()])
        observed = {email: raw["worker-a"][record["token"]]["user_id"] for email, record in baseline.items()}
        assert all(observed[email] == baseline[email]["target_user_id"] for email in baseline)
        save_private_json(RAW_EVIDENCE, observed)
        verify()
        return
    if args.preflight_raw and not args.apply_remaining:
        run_preflight()
        return
    if args.plan or not args.apply_remaining:
        assert stat_mode(BASELINE) == 0o600, "Baseline must be protected"
        sanitized_pending = [{key: value for key, value in row.items() if key != "token"} for row in pending]
        print(
            json.dumps(
                {"targets": sanitized_pending, "count": len(pending), "completed": len(baseline) - len(pending)},
                indent=2,
            )
        )
        return
    if args.confirm != "TRANSFER_REMAINING_51":
        raise RuntimeError("Requires explicit --confirm TRANSFER_REMAINING_51")
    assert stat_mode(BASELINE) == 0o600, "Baseline must be protected"
    assert len(baseline) == 53, "Original manifest size mismatch"
    token = admin_key()
    assert stat_mode(LOCK) in (None, 0o600)
    with LOCK.open("a") as handle:
        LOCK.chmod(0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        recovery = pending_requests(baseline)
        baseline, _, _, committed = run_preflight()
        pending = plan(baseline, committed)
        pending.sort(key=lambda row: (row["email"] not in recovery, row["original_user_id"] is None, row["email"]))
        assert 0 < args.max_transfers <= len(pending), (
            "Choose a nonzero bounded transfer count no larger than pending scope"
        )
        client = ApiClient(token)

        def get_user(email):
            return matching_user(all_users(token), email)

        def validate_user(user, email):
            assert_complete(token, user, False)

        selected = pending[: args.max_transfers]
        for target in selected:
            email = target["email"]
            get_current_key = lambda email=email: key_candidates(token)[email]
            get_attached_keys = lambda user_id: pages(
                token, f"/key/list?user_id={user_id}&return_full_object=true", "keys"
            )
            get_raw_owners = raw_owners
            result = reconcile_one(
                email,
                baseline[email],
                client,
                get_current_key,
                get_user,
                validate_user,
                get_attached_keys,
                get_raw_owners,
                owner_evidence_path=RAW_EVIDENCE,
                state_path=STATE,
                journal_path=JOURNAL,
            )
            print(json.dumps({"email": email, "result": result, "selected_batch_progress": len(selected)}))
        print(
            json.dumps(
                {
                    "completed_this_invocation": len(selected),
                    "next_command_required_for_remaining": len(pending) > len(selected),
                }
            )
        )


if __name__ == "__main__":
    main()
