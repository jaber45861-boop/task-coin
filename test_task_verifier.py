"""
Tests for the Task Verification Contract.

Covers:
  - Passed / failed / error verification results
  - Required verification context fields
  - Verifier does not mutate task state
  - Verifier does not complete a task
  - Compatibility with CompletionGate
  - Malformed / invalid verification input
  - Registry behavior

Run:
    python -m unittest test_task_verifier.py -v
"""

import os
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock

from config import CHANNELS
import db
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_verifier import (
    TaskVerifier,
    VerificationContext,
    clear_verifiers,
    get_verifier,
    register_verifier,
    verify_task,
)


# ── Test Verifiers ────────────────────────────────────────────────


class _PassVerifier(TaskVerifier):
    """Always returns PASSED."""

    def __init__(self):
        self.call_count = 0
        self.last_context: VerificationContext | None = None

    def verify(self, context: VerificationContext) -> VerificationResult:
        self.call_count += 1
        self.last_context = context
        return VerificationResult(status=VerificationStatus.PASSED)


class _FailVerifier(TaskVerifier):
    """Always returns FAILED with a reason."""

    def __init__(self, reason: str = "not subscribed"):
        self.reason = reason

    def verify(self, context: VerificationContext) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.FAILED, reason=self.reason)


class _ErrorVerifier(TaskVerifier):
    """Always returns ERROR."""

    def __init__(self, reason: str = "api timeout"):
        self.reason = reason

    def verify(self, context: VerificationContext) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.ERROR, reason=self.reason)


class _CrashVerifier(TaskVerifier):
    """Raises an exception to test error handling."""

    def verify(self, context: VerificationContext) -> VerificationResult:
        raise RuntimeError("verifier crashed")


class _BadReturnVerifier(TaskVerifier):
    """Returns a non-VerificationResult to test type safety."""

    def verify(self, context: VerificationContext) -> VerificationResult:  # type: ignore[return-value]
        return "not a VerificationResult"  # type: ignore[return-value]


class _StateSpyVerifier(TaskVerifier):
    """Verifier that tries to mutate state (should not succeed)."""

    def __init__(self):
        self.attempted_mutations: list[str] = []

    def verify(self, context: VerificationContext) -> VerificationResult:
        # Try to access DB — but should not modify anything
        user = db.get_user(context.user_id)
        task = db.get_task(context.task_id)
        utask = db.get_user_task(context.user_id, context.task_id)
        # Record what we read but don't change anything
        self.attempted_mutations.append(
            f"user={user is not None}, task={task is not None}, utask={utask is not None}"
        )
        return VerificationResult(status=VerificationStatus.PASSED)


class _ContextCaptureVerifier(TaskVerifier):
    """Captures the context for inspection in boundary tests."""

    def __init__(self):
        self.last_context: VerificationContext | None = None

    def verify(self, context: VerificationContext) -> VerificationResult:
        self.last_context = context
        return VerificationResult(status=VerificationStatus.PASSED)


# ── Tests ─────────────────────────────────────────────────────────


class TestVerificationContext(unittest.TestCase):
    """Tests for the VerificationContext dataclass."""

    def test_frozen(self):
        """VerificationContext is immutable."""
        ctx = VerificationContext(user_id=1, task_id=2, task_type="sub")
        with self.assertRaises(AttributeError):
            ctx.user_id = 999  # type: ignore[misc]

    def test_default_task_data(self):
        """Default task_data is an empty dict."""
        ctx = VerificationContext(user_id=1, task_id=2, task_type="sub")
        self.assertEqual(ctx.task_data, {})

    def test_custom_task_data(self):
        """task_data can be passed explicitly."""
        from task_verifier import FrozenDict
        ctx = VerificationContext(
            user_id=1, task_id=2, task_type="sub",
            task_data=FrozenDict({"title": "Join", "reward": 50}),
        )
        self.assertEqual(ctx.task_data["title"], "Join")
        self.assertEqual(ctx.task_data["reward"], 50)


class TestVerifierRegistry(unittest.TestCase):
    """Tests for verifier registration."""

    def setUp(self):
        clear_verifiers()

    def tearDown(self):
        clear_verifiers()

    def test_register_and_get(self):
        """Register a verifier and retrieve it."""
        v = _PassVerifier()
        register_verifier("subscribe", v)
        self.assertIs(get_verifier("subscribe"), v)

    def test_get_unregistered_returns_none(self):
        """Unregistered type returns None."""
        self.assertIsNone(get_verifier("visit"))

    def test_register_rejects_non_verifier(self):
        """Cannot register a non-TaskVerifier object."""
        with self.assertRaises(TypeError):
            register_verifier("subscribe", "not a verifier")  # type: ignore[arg-type]

    def test_register_rejects_plain_function(self):
        """Cannot register a plain function."""
        with self.assertRaises(TypeError):
            register_verifier("subscribe", lambda ctx: None)  # type: ignore[arg-type]

    def test_clear_verifiers(self):
        """clear_verifiers empties the registry."""
        register_verifier("subscribe", _PassVerifier())
        self.assertIsNotNone(get_verifier("subscribe"))
        clear_verifiers()
        self.assertIsNone(get_verifier("subscribe"))

    def test_multiple_types(self):
        """Different task types have separate verifiers."""
        v1 = _PassVerifier()
        v2 = _FailVerifier()
        register_verifier("subscribe", v1)
        register_verifier("visit", v2)
        self.assertIs(get_verifier("subscribe"), v1)
        self.assertIs(get_verifier("visit"), v2)


class TestVerifyTask(unittest.TestCase):
    """Tests for verify_task() entry point."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Join Channel",
            description="Subscribe to our channel",
            task_type="subscribe",
            reward=50,
            db_path=self.test_db_path,
        )
        clear_verifiers()

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

    # ── 1. Passed verification ──────────────────────────────────
    def test_passed_verification(self):
        """verify_task returns PASSED when verifier passes."""
        v = _PassVerifier()
        register_verifier("subscribe", v)
        result = verify_task(1001, self.task_id)
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.reason, "")
        self.assertEqual(v.call_count, 1)

    # ── 2. Failed verification ──────────────────────────────────
    def test_failed_verification(self):
        """verify_task returns FAILED when verifier fails."""
        register_verifier("subscribe", _FailVerifier("not subscribed"))
        result = verify_task(1001, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertEqual(result.reason, "not subscribed")

    # ── 3. Error result ─────────────────────────────────────────
    def test_error_result(self):
        """verify_task returns ERROR when verifier errors."""
        register_verifier("subscribe", _ErrorVerifier("network down"))
        result = verify_task(1001, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("network down", result.reason)

    # ── 4. Required verification context ────────────────────────
    def test_context_receives_correct_fields(self):
        """Verifier receives user_id, task_id, task_type, and task_data."""
        v = _ContextCaptureVerifier()
        register_verifier("subscribe", v)
        verify_task(1001, self.task_id)

        ctx = v.last_context
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.user_id, 1001)
        self.assertEqual(ctx.task_id, self.task_id)
        self.assertEqual(ctx.task_type, "subscribe")
        # task_data contains only verification-relevant data, not metadata
        # (title, description, reward are NOT in context)
        self.assertNotIn("title", ctx.task_data)
        self.assertNotIn("description", ctx.task_data)
        self.assertNotIn("reward", ctx.task_data)
        # expected_data and actual_data are FrozenDicts
        from task_verifier import FrozenDict
        self.assertIsInstance(ctx.expected_data, FrozenDict)
        self.assertIsInstance(ctx.actual_data, FrozenDict)
        self.assertIsInstance(ctx.task_data, FrozenDict)

    # ── 5. Verifier does not mutate task state ──────────────────
    def test_verifier_does_not_mutate_task_state(self):
        """Verifier reads state but does not modify tasks or user_tasks."""
        v = _StateSpyVerifier()
        register_verifier("subscribe", v)

        db.create_user_task(1001, self.task_id, self.test_db_path)
        task_before = db.get_task(self.task_id, self.test_db_path)
        utask_before = db.get_user_task(1001, self.task_id, self.test_db_path)

        verify_task(1001, self.task_id)

        task_after = db.get_task(self.task_id, self.test_db_path)
        utask_after = db.get_user_task(1001, self.task_id, self.test_db_path)

        self.assertEqual(task_before, task_after)
        self.assertEqual(utask_before, utask_after)
        self.assertTrue(len(v.attempted_mutations) > 0)

    # ── 6. Verifier does not complete a task ────────────────────
    def test_verifier_does_not_complete_task(self):
        """verify_task never transitions user_task to completed."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _PassVerifier())
        result = verify_task(1001, self.task_id)
        self.assertTrue(result.passed)

        # Status should still be 'started', not 'completed'
        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 7. Compatibility with CompletionGate ────────────────────
    def test_verify_then_complete_works(self):
        """verify_task result feeds into CompletionGate.complete() successfully."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _PassVerifier())
        verification = verify_task(1001, self.task_id)
        self.assertTrue(verification.passed)

        gate = CompletionGate()
        completed = gate.complete(1001, self.task_id, verification)
        self.assertTrue(completed)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_failed_verify_blocks_gate(self):
        """A FAILED verification blocks CompletionGate."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        register_verifier("subscribe", _FailVerifier())
        verification = verify_task(1001, self.task_id)
        self.assertFalse(verification.passed)

        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, self.task_id, verification)

        utask = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 8. Malformed / invalid verification input ──────────────
    def test_nonexistent_user_returns_error(self):
        """verify_task returns ERROR for non-existent user."""
        register_verifier("subscribe", _PassVerifier())
        result = verify_task(99999, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("99999", result.reason)

    def test_nonexistent_task_returns_error(self):
        """verify_task returns ERROR for non-existent task."""
        register_verifier("subscribe", _PassVerifier())
        result = verify_task(1001, 99999)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("99999", result.reason)

    def test_no_verifier_registered_returns_error(self):
        """verify_task returns ERROR when no verifier is registered for the type."""
        # No verifier registered
        result = verify_task(1001, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("No verifier registered", result.reason)

    def test_verifier_exception_returns_error(self):
        """verify_task catches verifier exceptions and returns ERROR."""
        register_verifier("subscribe", _CrashVerifier())
        result = verify_task(1001, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("crashed", result.reason)

    def test_verifier_wrong_return_type_returns_error(self):
        """verify_task handles a verifier returning wrong type."""
        register_verifier("subscribe", _BadReturnVerifier())
        result = verify_task(1001, self.task_id)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("invalid type", result.reason)


class TestVerifierDoesNotMutate(unittest.TestCase):
    """Extra isolation tests: verifier never touches user/task/user_task state."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(2001, "bob", "Bob")
        self.task_id = db.create_task(
            title="Visit Site",
            description="Go to website",
            task_type="visit",
            reward=10,
            db_path=self.test_db_path,
        )
        clear_verifiers()

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

    def test_verifier_does_not_modify_users(self):
        """Verifier does not change user records."""
        register_verifier("visit", _PassVerifier())
        user_before = db.get_user(2001)
        verify_task(2001, self.task_id)
        user_after = db.get_user(2001)
        self.assertEqual(user_before, user_after)

    def test_verifier_does_not_modify_task(self):
        """Verifier does not change task definitions."""
        register_verifier("visit", _PassVerifier())
        task_before = db.get_task(self.task_id, self.test_db_path)
        verify_task(2001, self.task_id)
        task_after = db.get_task(self.task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)

    def test_verifier_does_not_create_user_task(self):
        """Verifier does not create user_task records."""
        register_verifier("visit", _PassVerifier())
        self.assertIsNone(db.get_user_task(2001, self.task_id, self.test_db_path))
        verify_task(2001, self.task_id)
        self.assertIsNone(db.get_user_task(2001, self.task_id, self.test_db_path))


if __name__ == "__main__":
    unittest.main()
