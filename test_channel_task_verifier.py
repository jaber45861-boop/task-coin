"""
Tests for the Channel Task Verifier (MT-TASK-02).

Covers:
  - verifier is registered through the existing registry mechanism
  - task type `channel_subscription` resolves to ChannelTaskVerifier
    from `import task_verifier` alone (subprocess check)
  - valid member (member / administrator / creator) → PASSED
  - non-member (left / kicked / restricted) → FAILED
  - supergroup membership semantics (same as the subscription gate)
  - malformed / missing task_data fails safely → ERROR
  - unknown channel configuration fails safely → ERROR
  - Telegram / infrastructure failure → ERROR
  - client-supplied fields cannot override trusted task data
  - verifier mutates nothing and never completes a task
  - full pipeline: member → completed, non-member → stays started

No real Telegram API call is ever made — membership checkers are fakes.

Run:
    python -m pytest test_channel_task_verifier.py -v
    # or
    python -m unittest test_channel_task_verifier.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from telegram.error import TelegramError

from channel_task_verifier import (
    CHANNEL_TASK_TYPE,
    ChannelTaskVerifier,
    register_channel_task_verifier,
)
from completion_bridge import CompletionBridge
from config import CHANNELS, Channel
import db
from task_completion import VerificationResult, VerificationStatus
from task_start import TaskStartGate
from task_submission import TaskSubmissionService, SubmissionError
from task_verifier import (
    clear_verifiers,
    get_verifier,
    verify_task,
)

_USER_ID = 1001
_ADMIN_CHANNEL_ID = -1001234567890
_ATTACKER_CHANNEL_ID = -999999999


class ChannelTaskVerifierTestCase(unittest.TestCase):
    """Shared fixture: temp DB, trusted channel task, configured channels."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(_USER_ID, "alice", "Alice")
        self.task_id = db.create_task(
            title="Join Channel",
            description="Subscribe to our channel",
            task_type=CHANNEL_TASK_TYPE,
            reward=50,
            db_path=self.test_db_path,
            task_data=json.dumps({"channel_slug": "main"}),
        )
        CHANNELS["main"] = Channel(
            slug="main",
            channel_id=_ADMIN_CHANNEL_ID,
            username="main_user",
            title="Main Channel",
            required=True,
        )
        # Registry isolation: known state before every test.
        clear_verifiers()
        register_channel_task_verifier()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        clear_verifiers()
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    # ── Helpers ────────────────────────────────────────────────

    def _register_fake(self, status: str = "member"):
        """Register a verifier whose membership check is a fake.

        Returns the call list: [(channel_id, user_id), ...].
        """
        calls: list[tuple[int, int]] = []

        def checker(channel_id: int, user_id: int) -> str:
            calls.append((channel_id, user_id))
            return status

        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=checker)
        )
        return calls

    def _register_raising(self, exc: BaseException):
        """Register a verifier whose membership check raises *exc*."""
        calls: list[tuple[int, int]] = []

        def checker(channel_id: int, user_id: int) -> str:
            calls.append((channel_id, user_id))
            raise exc

        register_channel_task_verifier(
            ChannelTaskVerifier(membership_checker=checker)
        )
        return calls

    def _make_task(self, task_data) -> int:
        """Create an extra channel task with the given raw task_data."""
        raw = task_data if isinstance(task_data, str) or task_data is None \
            else json.dumps(task_data)
        return db.create_task(
            title="Channel Task",
            description="Join the channel",
            task_type=CHANNEL_TASK_TYPE,
            reward=25,
            db_path=self.test_db_path,
            task_data=raw,
        )

    def _start_task(self, task_id: int | None = None) -> None:
        task_id = self.task_id if task_id is None else task_id
        db.create_user_task(_USER_ID, task_id, self.test_db_path)
        db.update_user_task_status(
            _USER_ID, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )


# ── Registration ──────────────────────────────────────────────────


class TestRegistration(ChannelTaskVerifierTestCase):
    """The registry must resolve channel_subscription correctly."""

    def test_registered_on_import(self):
        """Importing channel_task_verifier registers the verifier."""
        verifier = get_verifier(CHANNEL_TASK_TYPE)
        self.assertIsNotNone(verifier)
        self.assertIsInstance(verifier, ChannelTaskVerifier)

    def test_task_type_resolves(self):
        """The documented task type string resolves to the verifier."""
        self.assertEqual(CHANNEL_TASK_TYPE, "channel_subscription")
        self.assertIs(
            type(get_verifier(CHANNEL_TASK_TYPE)), ChannelTaskVerifier
        )

    def test_task_verifier_import_registers_channel_subscription(self):
        """`import task_verifier` alone must resolve the channel verifier.

        Runs in a fresh interpreter so no direct import of
        channel_task_verifier can mask a missing registration.
        """
        repo_root = os.path.dirname(os.path.abspath(__file__))
        code = (
            "import task_verifier\n"
            "from task_verifier import get_verifier\n"
            "v = get_verifier('channel_subscription')\n"
            "assert v is not None, 'channel_subscription not registered'\n"
            "assert type(v).__name__ == 'ChannelTaskVerifier'\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_reregistration_after_clear(self):
        """Tests may clear the registry; re-registration restores it."""
        clear_verifiers()
        self.assertIsNone(get_verifier(CHANNEL_TASK_TYPE))
        register_channel_task_verifier()
        self.assertIsInstance(
            get_verifier(CHANNEL_TASK_TYPE), ChannelTaskVerifier
        )


# ── Membership → PASSED / FAILED ──────────────────────────────────


class TestMembershipResults(ChannelTaskVerifierTestCase):
    """Valid membership passes; missing membership fails."""

    def test_valid_member_passes(self):
        calls = self._register_fake("member")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.PASSED)
        # Checked against the ADMIN-configured channel, not client data
        self.assertEqual(calls, [(_ADMIN_CHANNEL_ID, _USER_ID)])

    def test_administrator_passes(self):
        self._register_fake("administrator")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.PASSED)

    def test_creator_passes(self):
        self._register_fake("creator")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.PASSED)

    def test_non_member_fails(self):
        calls = self._register_fake("left")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("not a member", result.reason)
        self.assertEqual(calls, [(_ADMIN_CHANNEL_ID, _USER_ID)])

    def test_kicked_fails(self):
        self._register_fake("kicked")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_restricted_fails(self):
        # Same semantics as subscription._is_chat_member (fail-closed)
        self._register_fake("restricted")
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_supergroup_membership_passes(self):
        """Channel/supergroup semantics match the subscription gate."""
        CHANNELS["group"] = Channel(
            slug="group",
            channel_id=-1009876543210,
            username="group_user",
            title="Super Group",
            required=True,
            chat_type="supergroup",
        )
        task_id = self._make_task({"channel_slug": "group"})
        calls = self._register_fake("member")
        result = verify_task(_USER_ID, task_id)
        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(calls, [(-1009876543210, _USER_ID)])


# ── Malformed / missing task_data ─────────────────────────────────


class TestMalformedTaskData(ChannelTaskVerifierTestCase):
    """Broken task configuration fails safely — never passes."""

    def _assert_error_without_check(self, task_id: int):
        calls = self._register_fake("member")
        result = verify_task(_USER_ID, task_id)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertEqual(calls, [], "membership must not be checked")

    def test_missing_task_data(self):
        task_id = self._make_task(None)
        self._assert_error_without_check(task_id)

    def test_invalid_json_task_data(self):
        task_id = self._make_task("{not json")
        self._assert_error_without_check(task_id)

    def test_task_data_not_a_dict(self):
        task_id = self._make_task(["main"])
        self._assert_error_without_check(task_id)

    def test_missing_channel_slug(self):
        task_id = self._make_task({"other_key": "main"})
        self._assert_error_without_check(task_id)

    def test_channel_slug_not_a_string(self):
        task_id = self._make_task({"channel_slug": 12345})
        self._assert_error_without_check(task_id)

    def test_channel_slug_empty(self):
        task_id = self._make_task({"channel_slug": "   "})
        self._assert_error_without_check(task_id)


# ── Unknown channel configuration ─────────────────────────────────


class TestUnknownChannelConfiguration(ChannelTaskVerifierTestCase):
    """A slug that does not resolve fails safely — never passes."""

    def test_unconfigured_slug_errors(self):
        task_id = self._make_task({"channel_slug": "does_not_exist"})
        calls = self._register_fake("member")
        result = verify_task(_USER_ID, task_id)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("not configured", result.reason)
        self.assertEqual(calls, [], "membership must not be checked")


# ── Telegram / infrastructure failures ────────────────────────────


class TestInfrastructureErrors(ChannelTaskVerifierTestCase):
    """Telegram/API failures map to ERROR per the verifier contract."""

    def test_telegram_error_is_error(self):
        self._register_raising(TelegramError("boom"))
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("Telegram membership check failed", result.reason)

    def test_generic_exception_is_error(self):
        self._register_raising(RuntimeError("network down"))
        result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    def test_default_checker_without_token_is_error(self):
        """The default live checker fails closed when no token exists."""
        clear_verifiers()
        register_channel_task_verifier()  # default (env-token) checker
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            result = verify_task(_USER_ID, self.task_id)
        self.assertEqual(result.status, VerificationStatus.ERROR)


# ── Client input cannot override trusted task data ────────────────


class TestClientCannotOverride(ChannelTaskVerifierTestCase):
    """Submission data must never determine the channel under check."""

    def test_submission_fields_cannot_override_channel(self):
        self._start_task()
        CHANNELS["attacker_channel"] = Channel(
            slug="attacker_channel",
            channel_id=_ATTACKER_CHANNEL_ID,
            username="attacker",
            title="Attacker Channel",
            required=True,
        )
        calls = self._register_fake("left")

        result = TaskSubmissionService.submit(
            _USER_ID,
            self.task_id,
            {
                "channel_slug": "attacker_channel",
                "channel_id": _ATTACKER_CHANNEL_ID,
                "channel_username": "attacker",
                "channel_title": "Attacker Channel",
                "verified": True,
                "is_member": True,
            },
        )
        # Real membership (left) decides — flags are ignored.
        self.assertEqual(result.status, VerificationStatus.FAILED)
        # The ADMIN-configured channel was checked, not the attacker's.
        self.assertEqual(calls, [(_ADMIN_CHANNEL_ID, _USER_ID)])
        # And nothing was completed.
        row = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_submission_forbidden_fields_still_rejected(self):
        """Existing submission guard keeps rejecting system fields."""
        self._start_task()
        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                _USER_ID, self.task_id, {"reward": 999}
            )


# ── Verifier purity + pipeline integration ────────────────────────


class TestVerifierPurityAndPipeline(ChannelTaskVerifierTestCase):
    """Verification mutates nothing; completion happens only via gates."""

    def test_verify_does_not_mutate_state(self):
        self._start_task()
        self._register_fake("member")
        task_before = db.get_task(self.task_id, self.test_db_path)
        utask_before = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)

        result = verify_task(_USER_ID, self.task_id)
        self.assertTrue(result.passed)

        task_after = db.get_task(self.task_id, self.test_db_path)
        utask_after = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)
        self.assertEqual(utask_before, utask_after)

    def test_pipeline_member_completes_task(self):
        self._start_task()
        self._register_fake("member")
        result = CompletionBridge.complete_after_verification(
            _USER_ID, self.task_id, {}
        )
        self.assertEqual(result.status, VerificationStatus.PASSED)
        row = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_pipeline_non_member_stays_started(self):
        self._start_task()
        self._register_fake("left")
        result = CompletionBridge.complete_after_verification(
            _USER_ID, self.task_id, {}
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)
        row = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_pipeline_api_failure_stays_started(self):
        self._start_task()
        self._register_raising(TelegramError("boom"))
        result = CompletionBridge.complete_after_verification(
            _USER_ID, self.task_id, {}
        )
        self.assertEqual(result.status, VerificationStatus.ERROR)
        row = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_start_gate_feeds_the_pipeline(self):
        """start_task() → submit → verifier → completion boundary."""
        gate = TaskStartGate()
        start = gate.start(_USER_ID, self.task_id)
        self.assertTrue(start.success)
        self._register_fake("member")
        result = CompletionBridge.complete_after_verification(
            _USER_ID, self.task_id, {}
        )
        self.assertTrue(result.passed)
        row = db.get_user_task(_USER_ID, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_result_contract_is_immutable(self):
        """Returned result is the existing frozen contract object."""
        self._register_fake("member")
        result = verify_task(_USER_ID, self.task_id)
        self.assertIsInstance(result, VerificationResult)
        with self.assertRaises(AttributeError):
            result.status = VerificationStatus.FAILED  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
