"""
Tests for Secure Task Completion Bridge (Micro-task 2.11).

Covers:
  - PASSED completes exactly once.
  - FAILED does not complete.
  - ERROR does not complete.
  - available task is not completed.
  - already completed task is rejected safely.
  - invalid user/task is rejected safely.
  - CompletionGate is the only completion path.
  - no direct DB mutation from CompletionBridge.
  - actual_data validation remains enforced by TaskSubmissionService.
  - forbidden client-controlled fields remain rejected.
  - verifier exceptions remain ERROR and never complete.
  - repeated submission after completion cannot mutate state.

Run:
    python -m unittest test_completion_bridge.py -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config import CHANNELS
import db
from completion_bridge import CompletionBridge, CompletionBridgeError
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_start import TaskStartGate
from task_verifier import (
    DeterministicTaskVerifier,
    TaskVerifier,
    clear_verifiers,
    register_verifier,
)


# ── Helper Verifiers ──────────────────────────────────────────────


class _PassVerifier(DeterministicTaskVerifier):
    """Always passes."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier(DeterministicTaskVerifier):
    """Always fails."""

    def verify(self, context):
        return VerificationResult(
            status=VerificationStatus.FAILED, reason="not subscribed"
        )


class _ErrorVerifier(DeterministicTaskVerifier):
    """Always returns ERROR."""

    def verify(self, context):
        return VerificationResult(
            status=VerificationStatus.ERROR, reason="api timeout"
        )


class _CrashVerifier(TaskVerifier):
    """Raises an exception during verification."""

    def verify(self, context):
        raise RuntimeError("simulated verifier crash")


class _BadReturnVerifier(TaskVerifier):
    """Returns a non-VerificationResult."""

    def verify(self, context):
        return "not a VerificationResult"


# ── Bridge Completion Tests ───────────────────────────────────────


class TestBridgeCompletion(unittest.TestCase):
    """Tests for PASSED completing via CompletionGate."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self.gate = TaskStartGate()

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

    def test_passed_completes_exactly_once(self):
        """PASSED result triggers CompletionGate and transitions to completed."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(row["completed_at"])

    def test_repeated_submission_after_completion_rejected(self):
        """After completion, the bridge rejects further submissions."""
        self.gate.start(1001, self.task_id)
        CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)
        self.assertIn("not in started state", result.reason)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)


# ── Bridge FAILED/ERROR Tests ─────────────────────────────────────


class TestBridgeFailedAndError(unittest.TestCase):
    """Tests that FAILED and ERROR do not complete the task."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        self.gate = TaskStartGate()

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

    def test_failed_does_not_complete(self):
        """FAILED result leaves task in STARTED."""
        register_verifier("deterministic", _FailVerifier())
        self.gate.start(1001, self.task_id)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "wrong"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_error_does_not_complete(self):
        """ERROR result leaves task in STARTED."""
        register_verifier("deterministic", _ErrorVerifier())
        self.gate.start(1001, self.task_id)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "data"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)


# ── Bridge State Guard Tests ──────────────────────────────────────


class TestBridgeStateGuards(unittest.TestCase):
    """Tests for invalid states being rejected safely."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self.gate = TaskStartGate()

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

    def test_available_task_not_completed(self):
        """Non-started (available) task is rejected safely."""
        # Create user_task in available state (no start)
        db.create_user_task(1001, self.task_id, self.test_db_path)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_AVAILABLE)

    def test_already_completed_task_rejected_safely(self):
        """Completed task returns FAILED without mutation."""
        self.gate.start(1001, self.task_id)
        CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        row_before = db.get_user_task(1001, self.task_id, self.test_db_path)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)

        row_after = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row_before["status"], row_after["status"])
        self.assertEqual(row_before["completed_at"], row_after["completed_at"])

    def test_invalid_user_rejected_safely(self):
        """Nonexistent user returns FAILED without mutation."""
        result = CompletionBridge.complete_after_verification(
            99999, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)

    def test_invalid_task_rejected_safely(self):
        """Nonexistent task returns FAILED without mutation."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, 99999, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)


# ── Bridge Security Tests ─────────────────────────────────────────


class TestBridgeSecurity(unittest.TestCase):
    """Tests for data boundary and security guarantees."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self.gate = TaskStartGate()

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

    def test_actual_data_validation_enforced(self):
        """Forbidden fields in actual_data are rejected by TaskSubmissionService."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"completed": True}
        )
        self.assertFalse(result.passed)
        self.assertIn("forbidden", result.reason.lower())

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_status_field_cannot_force_completion(self):
        """Client-supplied 'status' field is rejected."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"status": "completed"}
        )
        self.assertFalse(result.passed)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_reward_field_cannot_alter_task(self):
        """Client-supplied 'reward' field is rejected."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"reward": 99999}
        )
        self.assertFalse(result.passed)

        task = db.get_task(self.task_id, self.test_db_path)
        self.assertEqual(task["reward"], 100)

    def test_verifier_exception_never_completes(self):
        """A crashing verifier returns ERROR without completing."""
        clear_verifiers()
        register_verifier("deterministic", _CrashVerifier())
        self.gate.start(1001, self.task_id)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    def test_bad_verifier_return_never_completes(self):
        """A verifier returning non-VerificationResult returns ERROR without completing."""
        clear_verifiers()
        register_verifier("deterministic", _BadReturnVerifier())
        self.gate.start(1001, self.task_id)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)


# ── Bridge No-DB-Mutation Tests ───────────────────────────────────


class TestBridgeNoDBMutation(unittest.TestCase):
    """Tests verifying CompletionBridge itself never directly writes to DB."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self.gate = TaskStartGate()

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

    def test_no_direct_db_mutation(self):
        """CompletionBridge only delegates to CompletionGate, never writes directly."""
        self.gate.start(1001, self.task_id)

        original_complete = CompletionGate.complete
        calls = []

        def spy_complete(self_gate, uid, tid, verification):
            calls.append((uid, tid, verification))
            return original_complete(self_gate, uid, tid, verification)

        with patch.object(CompletionGate, "complete", spy_complete):
            result = CompletionBridge.complete_after_verification(
                1001, self.task_id, {"actual": "secret123"}
            )

        self.assertTrue(result.passed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1001)
        self.assertEqual(calls[0][1], self.task_id)

    def test_failed_result_never_calls_gate(self):
        """FAILED verification never invokes CompletionGate.complete()."""
        clear_verifiers()
        register_verifier("deterministic", _FailVerifier())
        self.gate.start(1001, self.task_id)

        original_complete = CompletionGate.complete
        calls = []

        def spy_complete(self_gate, uid, tid, verification):
            calls.append(True)
            return original_complete(self_gate, uid, tid, verification)

        with patch.object(CompletionGate, "complete", spy_complete):
            result = CompletionBridge.complete_after_verification(
                1001, self.task_id, {"actual": "wrong"}
            )

        self.assertFalse(result.passed)
        self.assertEqual(len(calls), 0)


# ── Full Lifecycle Tests ──────────────────────────────────────────


class TestBridgeLifecycle(unittest.TestCase):
    """Full internal lifecycle tests."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "secret123"}),
        )
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self.gate = TaskStartGate()

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

    def test_full_positive_lifecycle(self):
        """create → start → bridge(correct) → PASSED → COMPLETED."""
        self.gate.start(1001, self.task_id)

        row_started = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row_started["status"], db.USER_TASK_STATUS_STARTED)

        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertTrue(result.passed)

        row_completed = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row_completed["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_full_negative_lifecycle(self):
        """start → bridge(wrong) → FAILED → still STARTED → bridge again → FAILED → still STARTED."""
        clear_verifiers()
        register_verifier("deterministic", _FailVerifier())
        self.gate.start(1001, self.task_id)

        result1 = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "wrong"}
        )
        self.assertFalse(result1.passed)
        self.assertEqual(result1.status, VerificationStatus.FAILED)

        row1 = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row1["status"], db.USER_TASK_STATUS_STARTED)

        result2 = CompletionBridge.complete_after_verification(
            1001, self.task_id, {"actual": "also_wrong"}
        )
        self.assertFalse(result2.passed)
        self.assertEqual(result2.status, VerificationStatus.FAILED)

        row2 = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row2["status"], db.USER_TASK_STATUS_STARTED)

    def test_non_dict_actual_data_returns_error(self):
        """Non-dict actual_data returns ERROR safely."""
        self.gate.start(1001, self.task_id)
        result = CompletionBridge.complete_after_verification(
            1001, self.task_id, "not a dict"
        )
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("must be a dict", result.reason)


if __name__ == "__main__":
    unittest.main()
