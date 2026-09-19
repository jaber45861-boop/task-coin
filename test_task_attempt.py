"""
Tests for Task Attempt Policy Boundary (Micro-task 2.10).

Covers:
  Policy:
    - valid STARTED task → allowed
    - nonexistent user → rejected
    - nonexistent task → rejected
    - inactive task → rejected
    - missing user_task → rejected
    - AVAILABLE → rejected
    - COMPLETED → rejected
    - invalid status → rejected
    - policy performs no DB writes

  Submission integration:
    - rejected policy prevents verifier invocation
    - allowed policy invokes existing verifier
    - PASSED remains STARTED
    - FAILED remains STARTED
    - ERROR remains STARTED
    - Submission still never completes a task

  Security:
    - policy cannot be bypassed by actual_data
    - client fields cannot alter task state
    - policy cannot be used to complete a task
    - no CompletionGate call from policy/submission

Run:
    python -m unittest test_task_attempt.py -v
"""

import os
import tempfile
import unittest

import db
from task_attempt import AttemptResult, TaskAttemptPolicy
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_start import TaskStartGate
from task_submission import SubmissionError, TaskSubmissionService
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
            status=VerificationStatus.FAILED, reason="always fails"
        )


class _ErrorVerifier(DeterministicTaskVerifier):
    """Always returns ERROR."""

    def verify(self, context):
        return VerificationResult(
            status=VerificationStatus.ERROR, reason="always errors"
        )


class _ExceptionVerifier(TaskVerifier):
    """Raises an exception during verification."""

    def verify(self, context):
        raise RuntimeError("boom")


# ── Base Test Case ────────────────────────────────────────────────


class _AttemptPolicyBase(unittest.TestCase):
    """Base class with shared setup: temp DB, fixtures, teardown."""

    def setUp(self):
        self._orig_db_path = db.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        db.init_db(self._tmp.name)

        # Register a deterministic task type
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())

        # Create a user
        db.register_user(1001, "alice", "Alice")
        self.user_id = 1001

        # Create an active task with deterministic task_data
        import json
        self.task_id = db.create_task(
            title="Test Task",
            description="A test task",
            task_type="deterministic",
            reward=10,
            active=True,
            task_data=json.dumps({"expected": "correct_answer"}),
        )

        # Create an inactive task
        self.inactive_task_id = db.create_task(
            title="Inactive Task",
            description="An inactive task",
            task_type="deterministic",
            reward=5,
            active=False,
            task_data=json.dumps({"expected": "something"}),
        )

        # Create a user_task for the active task
        db.create_user_task(self.user_id, self.task_id)

    def tearDown(self):
        clear_verifiers()
        db.DB_PATH = self._orig_db_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass
        for suffix in ("-wal", "-shm"):
            p = self._tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)


# ── Policy Tests ──────────────────────────────────────────────────


class TestAttemptPolicyValidation(_AttemptPolicyBase):
    """Policy validation: user/task/state checks."""

    def test_valid_started_task_allowed(self):
        """A STARTED user_task should be allowed."""
        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertIsInstance(result, AttemptResult)
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason, "")

    def test_nonexistent_user_rejected(self):
        """A nonexistent user should be rejected."""
        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskAttemptPolicy.can_submit(99999, self.task_id)
        self.assertFalse(result.allowed)
        self.assertIn("not found", result.reason.lower())

    def test_nonexistent_task_rejected(self):
        """A nonexistent task should be rejected."""
        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskAttemptPolicy.can_submit(self.user_id, 99999)
        self.assertFalse(result.allowed)
        self.assertIn("not found", result.reason.lower())

    def test_inactive_task_rejected(self):
        """An inactive task should be rejected."""
        # Create user_task for inactive task (cannot start inactive task)
        db.create_user_task(self.user_id, self.inactive_task_id)
        result = TaskAttemptPolicy.can_submit(
            self.user_id, self.inactive_task_id
        )
        self.assertFalse(result.allowed)
        self.assertIn("not active", result.reason.lower())

    def test_missing_user_task_rejected(self):
        """A user_task that was never created should be rejected."""
        # User 1001 has no user_task for inactive_task_id without creation
        db.register_user(1002, "bob", "Bob")
        result = TaskAttemptPolicy.can_submit(1002, self.task_id)
        self.assertFalse(result.allowed)
        self.assertIn("no user_task", result.reason.lower())

    def test_available_task_rejected(self):
        """An AVAILABLE (not started) task should be rejected."""
        # user_task is created with status 'available' by default
        result = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertFalse(result.allowed)
        self.assertIn("available", result.reason.lower())

    def test_completed_task_rejected(self):
        """A COMPLETED task should be rejected."""
        TaskStartGate().start(self.user_id, self.task_id)
        # Complete the task via CompletionGate
        gate = CompletionGate()
        gate.complete(
            self.user_id,
            self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        result = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertFalse(result.allowed)
        self.assertIn("completed", result.reason.lower())

    def test_policy_performs_no_db_writes(self):
        """Policy should not modify any database table."""
        TaskStartGate().start(self.user_id, self.task_id)

        # Snapshot before
        ut_before = db.get_user_task(self.user_id, self.task_id)
        user_before = db.get_user(self.user_id)
        task_before = db.get_task(self.task_id)

        # Run policy
        TaskAttemptPolicy.can_submit(self.user_id, self.task_id)

        # Snapshot after
        ut_after = db.get_user_task(self.user_id, self.task_id)
        user_after = db.get_user(self.user_id)
        task_after = db.get_task(self.task_id)

        self.assertEqual(ut_before, ut_after)
        self.assertEqual(user_before, user_after)
        self.assertEqual(task_before, task_after)


# ── Submission Integration Tests ──────────────────────────────────


class TestAttemptPolicySubmissionIntegration(_AttemptPolicyBase):
    """Verify the policy is exercised by TaskSubmissionService."""

    def test_rejected_policy_prevents_verifier_invocation(self):
        """When policy rejects, verifier should never be called."""
        call_count = 0

        class _CountingVerifier(TaskVerifier):
            def verify(self, ctx):
                nonlocal call_count
                call_count += 1
                return VerificationResult(status=VerificationStatus.PASSED)

        clear_verifiers()
        register_verifier("deterministic", _CountingVerifier())

        # task is AVAILABLE (not started) → policy rejects
        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                self.user_id, self.task_id, {"actual": "correct_answer"}
            )
        self.assertEqual(call_count, 0)

    def test_allowed_policy_invokes_verifier(self):
        """When policy allows, the verifier should be invoked."""
        call_count = 0

        class _CountingVerifier(TaskVerifier):
            def verify(self, ctx):
                nonlocal call_count
                call_count += 1
                return VerificationResult(status=VerificationStatus.PASSED)

        clear_verifiers()
        register_verifier("deterministic", _CountingVerifier())

        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertEqual(call_count, 1)
        self.assertTrue(result.passed)

    def test_passed_leaves_task_started(self):
        """PASSED submission must not complete the task."""
        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertTrue(result.passed)
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_failed_leaves_task_started(self):
        """FAILED submission must not change task state."""
        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "wrong_answer"}
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_error_leaves_task_started(self):
        """ERROR submission must not change task state."""
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())

        TaskStartGate().start(self.user_id, self.task_id)
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "something"}
        )
        self.assertEqual(result.status, VerificationStatus.ERROR)
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_submission_never_completes_task(self):
        """Submission must never call CompletionGate or mark completed."""
        TaskStartGate().start(self.user_id, self.task_id)

        # Multiple submissions
        TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)
        self.assertIsNone(ut["completed_at"])


# ── Security Tests ────────────────────────────────────────────────


class TestAttemptPolicySecurity(_AttemptPolicyBase):
    """Security boundaries for the policy/submission integration."""

    def test_policy_cannot_be_bypassed_by_actual_data(self):
        """actual_data cannot circumvent the policy check."""
        # Task is AVAILABLE → policy rejects
        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                self.user_id,
                self.task_id,
                {"actual": "correct_answer", "status": "completed"},
            )

    def test_client_fields_cannot_alter_task_state(self):
        """Forbidden fields in actual_data are rejected."""
        TaskStartGate().start(self.user_id, self.task_id)

        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                self.user_id,
                self.task_id,
                {"actual": "x", "completed": True},
            )

        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                self.user_id,
                self.task_id,
                {"actual": "x", "reward": 9999},
            )

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_policy_cannot_be_used_to_complete_task(self):
        """The policy itself must not have any completion mechanism."""
        TaskStartGate().start(self.user_id, self.task_id)

        result = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertTrue(result.allowed)

        # Even after policy allows, task must remain STARTED
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_no_completiongate_call_from_policy(self):
        """Policy module must not import or call CompletionGate."""
        import inspect
        import task_attempt as mod
        source = inspect.getsource(mod)
        self.assertNotIn("CompletionGate", source)
        self.assertNotIn("complete(", source)

    def test_repeated_submissions_are_independent(self):
        """Each submission is an independent verification attempt."""
        TaskStartGate().start(self.user_id, self.task_id)

        # First submission: PASSED
        r1 = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertTrue(r1.passed)

        # Second submission: also PASSED (state unchanged)
        r2 = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertTrue(r2.passed)

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)


# ── Completion Integration Tests ──────────────────────────────────


class TestAttemptPolicyCompletionIntegration(_AttemptPolicyBase):
    """Verify that PASSED results can be explicitly completed via
    CompletionGate, and FAILED/ERROR results are rejected."""

    def test_passed_can_be_completed(self):
        """A PASSED result can be explicitly passed to CompletionGate."""
        TaskStartGate().start(self.user_id, self.task_id)

        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertTrue(result.passed)

        gate = CompletionGate()
        gate.complete(self.user_id, self.task_id, result)

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_failed_cannot_be_completed(self):
        """A FAILED result should be rejected by CompletionGate."""
        TaskStartGate().start(self.user_id, self.task_id)

        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "wrong_answer"}
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(self.user_id, self.task_id, result)

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_error_cannot_be_completed(self):
        """An ERROR result should be rejected by CompletionGate."""
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())

        TaskStartGate().start(self.user_id, self.task_id)

        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "something"}
        )
        self.assertEqual(result.status, VerificationStatus.ERROR)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(self.user_id, self.task_id, result)

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)


# ── Lifecycle Tests ───────────────────────────────────────────────


class TestAttemptPolicyLifecycle(_AttemptPolicyBase):
    """Full internal lifecycle tests."""

    def test_full_positive_lifecycle(self):
        """Create → Start → Policy allowed → Submit correct → PASSED →
        still STARTED → complete → COMPLETED."""
        # Start
        TaskStartGate().start(self.user_id, self.task_id)
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

        # Policy allows
        policy = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertTrue(policy.allowed)

        # Submit correct data
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        self.assertTrue(result.passed)

        # Still STARTED
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

        # Complete
        gate = CompletionGate()
        gate.complete(self.user_id, self.task_id, result)

        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_full_negative_lifecycle(self):
        """Start → Policy allowed → Submit wrong → FAILED →
        still STARTED → CompletionGate rejected → still STARTED."""
        # Start
        TaskStartGate().start(self.user_id, self.task_id)

        # Submit wrong data
        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "wrong_answer"}
        )
        self.assertEqual(result.status, VerificationStatus.FAILED)

        # Still STARTED
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

        # CompletionGate rejects FAILED
        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(self.user_id, self.task_id, result)

        # Still STARTED
        ut = db.get_user_task(self.user_id, self.task_id)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_policy_rejects_completed_task(self):
        """After completion, policy rejects further submissions."""
        TaskStartGate().start(self.user_id, self.task_id)

        result = TaskSubmissionService.submit(
            self.user_id, self.task_id, {"actual": "correct_answer"}
        )
        gate = CompletionGate()
        gate.complete(self.user_id, self.task_id, result)

        # Policy now rejects
        policy = TaskAttemptPolicy.can_submit(self.user_id, self.task_id)
        self.assertFalse(policy.allowed)
        self.assertIn("completed", policy.reason.lower())

        # Submission also rejects
        with self.assertRaises(SubmissionError):
            TaskSubmissionService.submit(
                self.user_id, self.task_id, {"actual": "correct_answer"}
            )


if __name__ == "__main__":
    unittest.main()
