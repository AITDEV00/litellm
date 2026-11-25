"""Exercise ownership transfer behavior with synthetic keys, users, and isolated journals."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import execute_key_transfer as runner


class KeyTransferRunnerTest(unittest.TestCase):
    def setUp(self):
        self.user = {"user_id": "target", "user_email": "person@ecouncil.ae", "user_role": "internal_user"}
        self.token = hashlib.sha256(b"synthetic-only-secret-not-a-real-credential").hexdigest()
        self.key = {
            "token": self.token,
            "user_id": "default_user_id",
            "key_alias": "person@ECOUNCIL.AE  ",
            "team_id": runner.DEVELOPER,
            "models": ["one"],
            "settings_updated_at": "before",
        }
        self.before = {
            "token": self.token,
            "alias": self.key["key_alias"],
            "original_user_id": "default_user_id",
            "target_user_id": "target",
            "original_key": copy.deepcopy(self.key),
        }
        self.raw_owner = "default_user_id"
        self.client = Mock()
        self.workspace = tempfile.TemporaryDirectory()
        self.path = Path(self.workspace.name)
        self.patch_journal = patch.object(runner, "committed_targets", lambda: {})
        self.patch_journal.start()

    def tearDown(self):
        self.patch_journal.stop()
        self.workspace.cleanup()

    def invoke(self):
        return runner.reconcile_one(
            "person@ecouncil.ae",
            self.before,
            self.client,
            lambda: self.key,
            lambda _: self.user,
            lambda *_: None,
            lambda user_id: [self.key] if self.key["user_id"] == user_id else [],
            lambda hashes: {
                worker: {self.token: {"user_id": self.raw_owner, "key_alias": self.key["key_alias"]}}
                for worker in ["pod-a", "pod-b"]
            },
            owner_evidence_path=self.path / "checked.json",
            state_path=self.path / "state.jsonl",
            journal_path=self.path / "journal.jsonl",
        )

    def assign_owner(self, owner):
        self.key.update(user_id=owner, settings_updated_at="after")
        self.raw_owner = owner

    def test_transfers_only_exact_original_hash_to_target_and_validates(self):
        self.client.transfer.side_effect = lambda token, owner: self.assign_owner(owner)
        self.assertEqual(self.invoke(), "transfer_verified")
        self.client.transfer.assert_called_once_with(self.token, "target")
        self.assertEqual(self.key["models"], ["one"])
        self.assertEqual(self.key["key_alias"], "person@ECOUNCIL.AE  ")
        events = [json.loads(line)["status"] for line in (self.path / "state.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["request_authorized", "raw_verified"])
        completed = json.loads((self.path / "journal.jsonl").read_text())
        self.assertEqual((completed["original_owner"], completed["new_owner"]), ("default_user_id", "target"))
        self.assertTrue(completed["owner_verified"])
        self.assertEqual((self.path / "journal.jsonl").stat().st_mode & 0o777, 0o600)

    def test_written_but_disconnected_request_is_not_retried(self):
        def fail_after_commit(token, owner):
            self.assign_owner(owner)
            raise TimeoutError("connection failed after commit")

        self.client.transfer.side_effect = fail_after_commit
        with self.assertRaises(TimeoutError):
            self.invoke()
        events = [json.loads(line)["status"] for line in (self.path / "state.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["request_authorized", "request_outcome_unknown"])
        self.client.transfer.reset_mock(side_effect=True)
        self.assertEqual(self.invoke(), "transfer_verified")
        self.client.transfer.assert_not_called()

    def test_unexpected_owner_halts_before_any_key_update(self):
        self.key["user_id"] = "unrelated-user"
        self.raw_owner = "unrelated-user"
        with self.assertRaisesRegex(AssertionError, "owner"):
            self.invoke()
        self.client.transfer.assert_not_called()

    def test_preserves_grants_while_null_original_owner_moves_to_target(self):
        self.key["user_id"] = "default_user_id"
        self.before["original_user_id"] = None
        self.before["original_key"]["user_id"] = None
        self.raw_owner = None
        self.client.transfer.side_effect = lambda token, owner: self.assign_owner(owner)
        self.invoke()
        self.assertEqual(self.key["models"], ["one"])
        raw_events = [json.loads(row) for row in (self.path / "journal.jsonl").read_text().splitlines()]
        self.assertIsNone(raw_events[-1]["original_owner"])

    def test_state_mismatch_after_write_is_not_success_journaled(self):
        def handle(token, owner):
            self.assign_owner(owner)
            self.key["models"] = []

        self.client.transfer.side_effect = handle
        with self.assertRaisesRegex(AssertionError, "configuration mismatch"):
            self.invoke()
        self.client.transfer.assert_called_once()
        self.assertFalse((self.path / "journal.jsonl").exists())

    def test_raw_owner_mismatch_cannot_claim_transfer_success(self):
        self.key["user_id"] = "target"
        self.raw_owner = "wrong-person"
        with patch.object(runner, "key_fingerprint", return_value={"unchanged": True}):
            with self.assertRaisesRegex(AssertionError, "raw owner mismatch"):
                self.invoke()
        self.assertFalse((self.path / "journal.jsonl").exists())
        self.client.transfer.assert_not_called()

    def test_api_request_contains_only_original_digest_and_target_uuid(self):
        with patch.object(
            runner,
            "api",
            return_value=(200, {"token": self.token, "user_id": "target"}),
        ) as request:
            client = runner.ApiClient("synthetic-admin-token")
            client.transfer(self.token, "target")
        request.assert_called_once_with(
            "synthetic-admin-token",
            "POST",
            "/key/update",
            {"key": self.token, "user_id": "target"},
        )

    def test_pending_plan_excludes_successful_targets_and_sorts(self):
        baseline = {
            "two": {"token": "two", "target_user_id": "two", "original_user_id": None},
            "one": {"token": "one", "target_user_id": "one", "original_user_id": "default_user_id"},
        }
        self.assertEqual([row["email"] for row in runner.plan(baseline, {"one": "one"})], ["two"])


if __name__ == "__main__":
    unittest.main()
