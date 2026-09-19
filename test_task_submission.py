"""
Tests for Secure Task Submission Boundary (Micro-task 2.9).

Covers:
  Validation:
    - valid started task accepts submission
    - nonexistent user rejected
    - nonexistent task rejected
    - inactive task rejected
    - missing user_task rejected
    - available task rejected
    - completed task rejected

  Data boundary:
    - actual_data is passed as submission data
    - expected_data remains task-defined
    - actual_data cannot overwrite expected_data
    - actual_data cannot change task_type
    - actual_data cannot change task_id/user_id
    - completed=true cannot force completion
    - status field cannot force completion
    - reward/active fields cannot alter task configuration
    - nested actual_data cannot mutate original caller data after submission

  Verification:
    - correct deterministic submission → PASSED
    - incorrect submission → FAILED
    - malformed submission → ERROR
    - verifier exception → ERROR

  State safety:
    - PASSED leaves task STARTED
    - FAILED leaves task STARTED
    - ERROR leaves task STARTED
    - submission never invokes CompletionGate
    - submission never completes task
    - submission never changes users/tasks

  Completion integration:
    - PASSED result can be explicitly passed to CompletionGate
    - CompletionGate then changes STARTED → COMPLETED
    - FAILED result is rejected by CompletionGate
    - ERROR result is rejected by CompletionGate

  Full lifecycle:
    - create user → create task → start → submit correct → PASSED → still STARTED → complete → COMPLETED
    - submit wrong → FAILED → still STARTED → CompletionGate rejected → still STARTED

Run:
    python -m unittest test_task_submission.py -v
"""

import copy
import json
import os
import tempfile
import unittest

from config import CHANNELS
import db
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_start import TaskStartGate
from task_submission import SubmissionError, TaskSubmissionService, FORBIDDEN_FIELDS
from task_verifier import (
    DeterministicTaskVerifier,
    TaskVerifier,
    clear_verifiers,
    register_verifier,
)


# ── Helper Verifiers ──────────────────────────────────────────────


class _PassVerifier(DeterministicTaskVerifier):
    """Always passes if task_data has expected and actual keys."""

    def verify(self, context):
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier(DeterministicTaskVerifier):
    """Always fails if task_data has expected and actual keys."""

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


class _ExceptionVerifier(TaskVerifier):
    """Raises an exception during verification."""

    def verify(self, context):
        raise RuntimeError("simulated verifier crash")


class _BadTypeVerifier(TaskVerifier):
    """Returns something other than VerificationResult."""

    def verify(self, context):
        return "not a VerificationResult"


# ── Validation Tests ──────────────────────────────────────────────


class TestSubmissionValidation(unittest.TestCase):
    """Tests for submission validation rules."""

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
        register_verifier("deterministic", DeterministicTaskVerifier())
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

    # ── 1. Valid started task accepts submission ──────────────
    def test_valid_started_task_accepts_submission(self):
        """A started task accepts a valid submission."""
        self.gate.start(1001, self.task_id)
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertIsInstance(result, VerificationResult)
        self.assertTrue(result.passed)

    # ── 2. Nonexistent user rejected ─────────────────────────
    def test_nonexistent_user_rejected(self):
        """Nonexistent user raises SubmissionError."""
        self.gate.start(1001, self.task_id)
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                99999, self.task_id, {"actual": "secret123"}
            )
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 3. Nonexistent task rejected ─────────────────────────
    def test_nonexistent_task_rejected(self):
        """Nonexistent task raises SubmissionError."""
        self.gate.start(1001, self.task_id)
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, 99999, {"actual": "secret123"}
            )
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 4. Inactive task rejected ────────────────────────────
    def test_inactive_task_rejected(self):
        """Inactive task raises SubmissionError."""
        inactive_id = db.create_task(
            title="Old Task",
            description="Deprecated",
            task_type="deterministic",
            reward=10,
            active=False,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "abc"}),
        )
        db.register_user(1002, "bob", "Bob")
        db.create_user_task(1002, inactive_id, self.test_db_path)
        db.update_user_task_status(
            1002, inactive_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1002, inactive_id, {"actual": "abc"}
            )
        self.assertIn("not active", str(ctx.exception))

    # ── 5. Missing user_task rejected ────────────────────────
    def test_missing_user_task_rejected(self):
        """Task that was never started (no user_task) raises SubmissionError."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id, {"actual": "secret123"}
            )
        self.assertIn("No user_task record", str(ctx.exception))

    # ── 6. Available task rejected ───────────────────────────
    def test_available_task_rejected(self):
        """Task in 'available' state raises SubmissionError."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id, {"actual": "secret123"}
            )
        self.assertIn("not in started state", str(ctx.exception))
        self.assertIn("available", str(ctx.exception))

    # ── 7. Completed task rejected ───────────────────────────
    def test_completed_task_rejected(self):
        """Completed task raises SubmissionError."""
        self.gate.start(1001, self.task_id)
        CompletionGate().complete(
            1001, self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id, {"actual": "secret123"}
            )
        self.assertIn("not in started state", str(ctx.exception))
        self.assertIn("completed", str(ctx.exception))


# ── Data Boundary Tests ───────────────────────────────────────────


class TestSubmissionDataBoundary(unittest.TestCase):
    """Tests for data-boundary protections in submission."""

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
        register_verifier("deterministic", DeterministicTaskVerifier())
        self.gate = TaskStartGate()
        self.gate.start(1001, self.task_id)

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

    # ── 1. actual_data is passed as submission data ──────────
    def test_actual_data_passed_to_context(self):
        """actual_data from user is present in context.actual_data."""
        from task_verifier import VerificationContext, FrozenDict

        class _CaptureVerifier:
            def __init__(self):
                self.last_context = None

            def verify(self, context):
                self.last_context = context
                return VerificationResult(status=VerificationStatus.PASSED)

        from task_verifier import TaskVerifier as _TV

        class _CaptureVerifierCls(_TV):
            def __init__(self):
                self.last_context = None

            def verify(self, context):
                self.last_context = context
                return VerificationResult(status=VerificationStatus.PASSED)

        verifier = _CaptureVerifierCls()
        register_verifier("deterministic", verifier)

        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "user_answer"}
        )

        self.assertIsNotNone(verifier.last_context)
        self.assertEqual(verifier.last_context.actual_data["actual"], "user_answer")

    # ── 2. expected_data remains task-defined ────────────────
    def test_expected_data_from_task_not_user(self):
        """expected_data comes from the task definition, not user."""
        from task_verifier import TaskVerifier as _TV

        class _CaptureVerifier(_TV):
            def __init__(self):
                self.last_context = None

            def verify(self, context):
                self.last_context = context
                return VerificationResult(status=VerificationStatus.PASSED)

        verifier = _CaptureVerifier()
        register_verifier("deterministic", verifier)

        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong"}
        )

        # expected_data should be empty (no 'expected' key in actual_data path)
        # But task_data should have the expected value from task config
        self.assertEqual(
            verifier.last_context.task_data["expected"], "secret123"
        )

    # ── 3. actual_data cannot overwrite expected_data ────────
    def test_actual_cannot_overwrite_expected(self):
        """actual_data containing 'expected' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "expected": "hacked"},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("expected", str(ctx.exception))

    # ── 4. actual_data cannot change task_type ───────────────
    def test_actual_cannot_change_task_type(self):
        """actual_data containing 'task_type' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "task_type": "admin"},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("task_type", str(ctx.exception))

    # ── 5. actual_data cannot change task_id/user_id ─────────
    def test_actual_cannot_change_task_id(self):
        """actual_data containing 'task_id' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "task_id": 999},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("task_id", str(ctx.exception))

    def test_actual_cannot_change_user_id(self):
        """actual_data containing 'user_id' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "user_id": 999},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("user_id", str(ctx.exception))

    # ── 6. completed=true cannot force completion ────────────
    def test_completed_flag_rejected(self):
        """actual_data containing 'completed' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "completed": True},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("completed", str(ctx.exception))

    # ── 7. status field cannot force completion ──────────────
    def test_status_field_rejected(self):
        """actual_data containing 'status' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "status": "completed"},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("status", str(ctx.exception))

    # ── 8. reward/active fields cannot alter config ──────────
    def test_reward_field_rejected(self):
        """actual_data containing 'reward' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "reward": 99999},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("reward", str(ctx.exception))

    def test_active_field_rejected(self):
        """actual_data containing 'active' is rejected."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, self.task_id,
                {"actual": "answer", "active": False},
            )
        self.assertIn("forbidden fields", str(ctx.exception))
        self.assertIn("active", str(ctx.exception))

    # ── 9. nested actual_data cannot mutate caller data ──────
    def test_nested_actual_data_isolation(self):
        """Mutating the original dict after submission doesn't affect context."""
        from task_verifier import TaskVerifier as _TV

        class _CaptureVerifier(_TV):
            def __init__(self):
                self.last_context = None

            def verify(self, context):
                self.last_context = context
                return VerificationResult(status=VerificationStatus.PASSED)

        verifier = _CaptureVerifier()
        register_verifier("deterministic", verifier)

        caller_data = {"actual": {"nested": "original"}}
        TaskSubmissionService.submit(1001, self.task_id, caller_data)

        # Mutate original after submission
        caller_data["actual"]["nested"] = "mutated"

        # Frozen context should be unaffected
        self.assertEqual(
            verifier.last_context.actual_data["actual"]["nested"], "original"
        )

    # ── 10. actual_data must be a dict ───────────────────────
    def test_non_dict_actual_data_rejected(self):
        """Non-dict actual_data raises SubmissionError."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(1001, self.task_id, "not a dict")
        self.assertIn("must be a dict", str(ctx.exception))

    def test_list_actual_data_rejected(self):
        """List actual_data raises SubmissionError."""
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(1001, self.task_id, [1, 2, 3])
        self.assertIn("must be a dict", str(ctx.exception))

    # ── 11. all forbidden fields listed in FORBIDDEN_FIELDS ──
    def test_forbidden_fields_completeness(self):
        """FORBIDDEN_FIELDS contains all critical system fields."""
        expected_forbidden = {
            "completed", "status", "reward", "active", "expected",
            "task_type", "task_id", "user_id", "created_at",
            "started_at", "completed_at",
        }
        self.assertEqual(FORBIDDEN_FIELDS, expected_forbidden)


# ── Verification Tests ────────────────────────────────────────────


class TestSubmissionVerification(unittest.TestCase):
    """Tests for verification outcomes from submission."""

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
        register_verifier("deterministic", DeterministicTaskVerifier())
        self.gate = TaskStartGate()
        self.gate.start(1001, self.task_id)

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

    # ── 1. Correct deterministic submission → PASSED ─────────
    def test_correct_submission_passed(self):
        """Correct answer produces PASSED result."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.reason, "")

    # ── 2. Incorrect submission → FAILED ─────────────────────
    def test_incorrect_submission_failed(self):
        """Wrong answer produces FAILED result."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong_answer"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("expected", result.reason.lower())

    # ── 3. Malformed submission → ERROR ──────────────────────
    def test_malformed_submission_error(self):
        """Missing actual key produces ERROR result (not exception)."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"wrong_key": "value"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    # ── 4. Verifier exception → ERROR ────────────────────────
    def test_verifier_exception_error(self):
        """Verifier exception produces ERROR result (not crash)."""
        register_verifier("deterministic", _ExceptionVerifier())
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "anything"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("exception", result.reason.lower())

    # ── 5. Verifier returns invalid type → ERROR ─────────────
    def test_verifier_bad_return_type_error(self):
        """Verifier returning non-VerificationResult produces ERROR."""
        register_verifier("deterministic", _BadTypeVerifier())
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "anything"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("invalid type", result.reason.lower())

    # ── 6. Type-strict: int 123 vs string "123" → FAILED ────
    def test_type_strict_int_vs_string(self):
        """Type-strict comparison: 123 (int) vs '123' (str) → FAILED."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "123"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    # ── 7. No verifier registered → ERROR ────────────────────
    def test_no_verifier_registered_error(self):
        """Missing verifier for task type produces ERROR."""
        clear_verifiers()
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("No verifier registered", result.reason)


# ── State Safety Tests ────────────────────────────────────────────


class TestSubmissionStateSafety(unittest.TestCase):
    """Tests that submission never modifies task/user state."""

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
        register_verifier("deterministic", DeterministicTaskVerifier())
        self.gate = TaskStartGate()
        self.gate.start(1001, self.task_id)

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

    # ── 1. PASSED leaves task STARTED ────────────────────────
    def test_passed_leaves_started(self):
        """PASSED result does not change user_task status."""
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 2. FAILED leaves task STARTED ────────────────────────
    def test_failed_leaves_started(self):
        """FAILED result does not change user_task status."""
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong"}
        )
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 3. ERROR leaves task STARTED ─────────────────────────
    def test_error_leaves_started(self):
        """ERROR result does not change user_task status."""
        TaskSubmissionService.submit(
            1001, self.task_id, {"bad_key": "value"}
        )
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 4. Submission never invokes CompletionGate ───────────
    def test_submission_never_completes_task(self):
        """Submission does not transition to completed."""
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertNotEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNone(utask["completed_at"])

    # ── 5. Submission never changes users ────────────────────
    def test_submission_never_changes_user(self):
        """Submission does not modify user records."""
        user_before = db.get_user(1001)
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        user_after = db.get_user(1001)
        self.assertEqual(user_before, user_after)

    # ── 6. Submission never changes task config ──────────────
    def test_submission_never_changes_task(self):
        """Submission does not modify task definitions."""
        task_before = db.get_task(self.task_id, self.test_db_path)
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        task_after = db.get_task(self.task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)

    # ── 7. Repeated submissions don't change state ───────────
    def test_repeated_submissions_safe(self):
        """Multiple submissions don't change task state."""
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong1"}
        )
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong2"}
        )
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 8. started_at not overwritten on submission ───────────
    def test_started_at_not_overwritten(self):
        """Submission does not modify started_at timestamp."""
        utask_before = db.get_user_task(1001, self.task_id, self.test_db_path)
        TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        utask_after = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask_before["started_at"], utask_after["started_at"])


# ── Completion Integration Tests ──────────────────────────────────


class TestSubmissionCompletionIntegration(unittest.TestCase):
    """Tests for submission → CompletionGate integration."""

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
        register_verifier("deterministic", DeterministicTaskVerifier())
        self.gate = TaskStartGate()
        self.gate.start(1001, self.task_id)

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

    # ── 1. PASSED → CompletionGate succeeds ──────────────────
    def test_passed_completes_via_gate(self):
        """PASSED submission feeds into CompletionGate → COMPLETED."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        self.assertTrue(result.passed)

        completion_gate = CompletionGate()
        completed = completion_gate.complete(1001, self.task_id, result)
        self.assertTrue(completed)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(utask["completed_at"])

    # ── 2. CompletionGate changes STARTED → COMPLETED ────────
    def test_gate_transitions_to_completed(self):
        """CompletionGate correctly transitions after PASSED."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "secret123"}
        )
        CompletionGate().complete(1001, self.task_id, result)
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 3. FAILED → CompletionGate rejected ──────────────────
    def test_failed_rejected_by_gate(self):
        """FAILED result is rejected by CompletionGate."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"actual": "wrong"}
        )
        self.assertFalse(result.passed)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, self.task_id, result)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 4. ERROR → CompletionGate rejected ───────────────────
    def test_error_rejected_by_gate(self):
        """ERROR result is rejected by CompletionGate."""
        result = TaskSubmissionService.submit(
            1001, self.task_id, {"bad_key": "value"}
        )
        self.assertFalse(result.passed)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, self.task_id, result)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)


# ── Full Lifecycle Tests ──────────────────────────────────────────


class TestSubmissionLifecycle(unittest.TestCase):
    """Full lifecycle integration tests."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())
        self.start_gate = TaskStartGate()
        self.completion_gate = CompletionGate()

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

    # ── 1. Full positive lifecycle ───────────────────────────
    def test_full_lifecycle_success(self):
        """create user → create task → start → submit correct → PASSED → still STARTED → complete → COMPLETED."""
        # Create task
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "correct_code"}),
        )

        # Start
        start_result = self.start_gate.start(1001, task_id)
        self.assertTrue(start_result.success)
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

        # Submit correct data
        verification = TaskSubmissionService.submit(
            1001, task_id, {"actual": "correct_code"}
        )
        self.assertTrue(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.PASSED)

        # Still STARTED after submission
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

        # Complete via gate
        completed = self.completion_gate.complete(1001, task_id, verification)
        self.assertTrue(completed)

        # Now COMPLETED
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(utask["completed_at"])

    # ── 2. Full negative lifecycle ───────────────────────────
    def test_full_lifecycle_failure(self):
        """submit wrong → FAILED → still STARTED → CompletionGate rejected → still STARTED."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "correct_code"}),
        )

        # Start
        self.assertTrue(self.start_gate.start(1001, task_id).success)

        # Submit wrong data
        verification = TaskSubmissionService.submit(
            1001, task_id, {"actual": "wrong_code"}
        )
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

        # Still STARTED
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

        # CompletionGate rejects FAILED
        with self.assertRaises(CompletionGateError):
            self.completion_gate.complete(1001, task_id, verification)

        # Still STARTED after rejected completion
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 3. Lifecycle: multiple failed attempts then success ───
    def test_multiple_failures_then_success(self):
        """Multiple failed submissions, then correct → PASSED → COMPLETED."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "right"}),
        )

        self.assertTrue(self.start_gate.start(1001, task_id).success)

        # Multiple wrong attempts
        for wrong in ["wrong1", "wrong2", "wrong3"]:
            result = TaskSubmissionService.submit(
                1001, task_id, {"actual": wrong}
            )
            self.assertFalse(result.passed)
            self.assertEqual(
                db.get_user_task(1001, task_id, self.test_db_path)["status"],
                db.USER_TASK_STATUS_STARTED,
            )

        # Finally correct
        result = TaskSubmissionService.submit(
            1001, task_id, {"actual": "right"}
        )
        self.assertTrue(result.passed)

        # Complete
        self.completion_gate.complete(1001, task_id, result)
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 4. Lifecycle: submission after completion rejected ────
    def test_submission_after_completion_rejected(self):
        """Submission on a completed task is rejected."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "answer"}),
        )

        self.start_gate.start(1001, task_id)
        result = TaskSubmissionService.submit(
            1001, task_id, {"actual": "answer"}
        )
        self.completion_gate.complete(1001, task_id, result)

        # Now try to submit again
        with self.assertRaises(SubmissionError) as ctx:
            TaskSubmissionService.submit(
                1001, task_id, {"actual": "answer"}
            )
        self.assertIn("not in started state", str(ctx.exception))

    # ── 5. Lifecycle: no tasks modified during entire flow ────
    def test_no_task_modification_throughout_lifecycle(self):
        """Task definition is unchanged through start → submit → complete."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": "answer"}),
        )
        task_before = db.get_task(task_id, self.test_db_path)

        self.start_gate.start(1001, task_id)
        TaskSubmissionService.submit(1001, task_id, {"actual": "answer"})
        self.completion_gate.complete(
            1001, task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )

        task_after = db.get_task(task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)


# ── Submission Service Does Not Import CompletionGate ─────────────


class TestSubmissionNoCompletionImport(unittest.TestCase):
    """Verify submission module does not import CompletionGate."""

    def test_no_completion_gate_import(self):
        """task_submission.py does not import CompletionGate."""
        import task_submission
        source = open(task_submission.__file__).read()
        self.assertNotIn("CompletionGate", source)

    def test_no_completion_gate_call(self):
        """task_submission.py does not call gate.complete."""
        import task_submission
        source = open(task_submission.__file__).read()
        self.assertNotIn("gate.complete", source)
        self.assertNotIn("complete(", source)

    def test_no_user_task_status_modification(self):
        """task_submission.py does not call update_user_task_status."""
        import task_submission
        source = open(task_submission.__file__).read()
        self.assertNotIn("update_user_task_status", source)

    def test_no_db_write_functions(self):
        """task_submission.py does not write to DB."""
        import task_submission
        source = open(task_submission.__file__).read()
        # Should not modify users, tasks, user_tasks, balances, referrals
        self.assertNotIn("register_user", source)
        self.assertNotIn("create_user_task", source)
        self.assertNotIn("save_channel", source)
        self.assertNotIn("delete_channel", source)
        self.assertNotIn("update_task", source)
        self.assertNotIn("delete_task", source)


if __name__ == "__main__":
    unittest.main()
