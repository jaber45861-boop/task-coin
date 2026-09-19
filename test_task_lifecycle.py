"""
Comprehensive tests for the Task Lifecycle Orchestrator (Micro-task 2.12).

Verifies delegation to the real approved modules:
    TaskStartGate, CompletionBridge, CompletionGate.

Run:
    python -m pytest test_task_lifecycle.py -v
    # or
    python -m unittest test_task_lifecycle -v
"""

import ast
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import db
from config import CHANNELS
from task_start import TaskStartGate, StartResult, StartGateError
from task_attempt import TaskAttemptPolicy
from task_submission import TaskSubmissionService, SubmissionError
from task_verifier import (
    DeterministicTaskVerifier,
    TaskVerifier,
    VerificationContext,
    clear_verifiers,
    register_verifier,
)
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from completion_bridge import CompletionBridge, CompletionBridgeError
from task_lifecycle import TaskLifecycle


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


# ── Setup Helpers ─────────────────────────────────────────────────


def _setup_db(test_db_path: str) -> None:
    """Initialize a fresh test database. db.DB_PATH must already be set."""
    db.init_db(test_db_path)
    db.register_user(100, "user100", "User 100")
    db.register_user(200, "user200", "User 200")
    db.register_user(999, "user999", "User 999")


def _create_test_task(
    title: str = "Test Task",
    task_type: str = "deterministic",
    active: bool = True,
    task_data: str | None = None,
) -> int:
    """Create a test task and return its ID."""
    return db.create_task(
        title=title,
        description="Test description",
        task_type=task_type,
        reward=10,
        active=active,
        task_data=task_data,
    )


def _get_code_only(module_path: str) -> str:
    """Extract only executable code from a Python file (strip docstrings/comments).

    This is used for architecture tests that must verify no forbidden
    business logic appears in executable code, while allowing those words
    in docstrings that describe what the module must NOT contain.
    """
    with open(module_path) as f:
        source = f.read()
    tree = ast.parse(source)
    code_lines = source.splitlines()
    # Collect line ranges that are docstrings (module, class, function)
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, (ast.Constant, ast.Str))):
                # Get the docstring line range
                doc_node = node.body[0].value
                start = getattr(doc_node, 'lineno', None)
                end = getattr(doc_node, 'end_lineno', None)
                if start is not None and end is not None:
                    for i in range(start, end + 1):
                        docstring_lines.add(i)
    # Build code-only source: keep only non-docstring lines
    non_docstring = []
    for i, line in enumerate(code_lines, 1):
        if i not in docstring_lines:
            non_docstring.append(line)
    return "\n".join(non_docstring)


# ── Test Classes ──────────────────────────────────────────────────


class TestStart(unittest.TestCase):
    """START tests."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        _setup_db(self._db)
        self._lifecycle = TaskLifecycle()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)
        CHANNELS.clear()

    def test_valid_available_task_starts(self):
        tid = _create_test_task()
        result = self._lifecycle.start_task(100, tid)
        self.assertTrue(result.success)
        self.assertEqual(result.status, db.USER_TASK_STATUS_STARTED)

    def test_nonexistent_user_rejected(self):
        tid = _create_test_task()
        with self.assertRaises(StartGateError) as ctx:
            self._lifecycle.start_task(999999, tid)
        self.assertIn("not found", str(ctx.exception))

    def test_nonexistent_task_rejected(self):
        with self.assertRaises(StartGateError) as ctx:
            self._lifecycle.start_task(100, 999999)
        self.assertIn("not found", str(ctx.exception))

    def test_inactive_task_rejected(self):
        tid = _create_test_task(active=False)
        with self.assertRaises(StartGateError) as ctx:
            self._lifecycle.start_task(100, tid)
        self.assertIn("not active", str(ctx.exception))

    def test_already_started_rejected(self):
        tid = _create_test_task()
        self._lifecycle.start_task(100, tid)
        with self.assertRaises(StartGateError) as ctx:
            self._lifecycle.start_task(100, tid)
        self.assertIn("already started", str(ctx.exception))

    def test_already_completed_rejected(self):
        tid = _create_test_task(
            task_data='{"expected": "secret123"}'
        )
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"actual": "secret123"})
        with self.assertRaises(StartGateError) as ctx:
            self._lifecycle.start_task(100, tid)
        self.assertIn("already completed", str(ctx.exception))


class TestSubmission(unittest.TestCase):
    """SUBMISSION tests."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self._db
        _setup_db(self._db)
        clear_verifiers()
        register_verifier("deterministic", _PassVerifier())
        self._lifecycle = TaskLifecycle()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())
        CHANNELS.clear()

    def test_started_passed_completes(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"actual": "secret123"})
        self.assertEqual(result.status, VerificationStatus.PASSED)
        # Verify task is COMPLETED
        ut = db.get_user_task(100, tid)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_COMPLETED)

    def test_started_failed_remains_started(self):
        clear_verifiers()
        register_verifier("deterministic", _FailVerifier())
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"actual": "wrong"})
        self.assertEqual(result.status, VerificationStatus.FAILED)
        ut = db.get_user_task(100, tid)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_started_error_remains_started(self):
        clear_verifiers()
        register_verifier("deterministic", _ErrorVerifier())
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.ERROR)
        ut = db.get_user_task(100, tid)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_available_task_cannot_submit(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        result = self._lifecycle.submit_task(100, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.passed)

    def test_completed_task_cannot_submit(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"actual": "secret123"})
        result = self._lifecycle.submit_task(100, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.passed)

    def test_invalid_user_task_rejected(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(999, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.passed)

    def test_non_dict_actual_data_rejected(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, "not a dict")
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("must be a dict", result.reason)

    def test_forbidden_fields_rejected(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(
            100, tid, {"actual": "test", "reward": 50}
        )
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("forbidden fields", result.reason.lower())

    def test_verifier_exception_remains_error(self):
        clear_verifiers()

        class _CrashVerifierLocal(TaskVerifier):
            def verify(self, context):
                raise RuntimeError("simulated crash")

        register_verifier("deterministic", _CrashVerifierLocal())
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("exception", result.reason.lower())
        ut = db.get_user_task(100, tid)
        self.assertEqual(ut["status"], db.USER_TASK_STATUS_STARTED)

    def test_repeated_submission_after_completion_rejected(self):
        tid = _create_test_task(task_data='{"expected": "secret123"}')
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"actual": "secret123"})
        result = self._lifecycle.submit_task(100, tid, {"actual": "test"})
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertFalse(result.passed)


class TestArchitecture(unittest.TestCase):
    """ARCHITECTURE tests — verify delegation to approved modules."""

    def test_task_lifecycle_imports_approved_modules(self):
        """TaskLifecycle must import the approved modules."""
        import task_lifecycle as tl_mod
        src = open(tl_mod.__file__).read()
        self.assertIn("from task_start import", src)
        self.assertIn("from completion_bridge import", src)
        self.assertIn("from task_completion import", src)

    def test_task_lifecycle_does_not_import_task_components(self):
        """TaskLifecycle must not import task_components.py."""
        import task_lifecycle as tl_mod
        src = open(tl_mod.__file__).read()
        self.assertNotIn("import task_components", src)
        self.assertNotIn("from task_components", src)

    def test_no_direct_db_mutation(self):
        """TaskLifecycle must not directly write to user_tasks."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__)
        self.assertNotIn("db.update_user_task_status", code)
        self.assertNotIn("INSERT INTO user_tasks", code)
        self.assertNotIn("UPDATE user_tasks", code)

    def test_no_direct_sqlite3_connection(self):
        """TaskLifecycle must not create sqlite3 connections."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__)
        self.assertNotIn("sqlite3.connect", code)

    def test_completion_bridge_only_path(self):
        """CompletionBridge is the only path to COMPLETED status."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__)
        # Must use CompletionBridge for completion
        self.assertIn("CompletionBridge", code)
        # Must NOT directly call CompletionGate
        self.assertNotIn("CompletionGate().complete", code)

    def test_delegates_to_task_start_gate(self):
        """TaskLifecycle.start_task delegates to TaskStartGate."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__)
        self.assertIn("TaskStartGate", code)
        self.assertIn("_start_gate.start", code)

    def test_delegates_to_completion_bridge(self):
        """TaskLifecycle.submit_task delegates to CompletionBridge."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__)
        self.assertIn("CompletionBridge.complete_after_verification", code)

    def test_no_reward_logic(self):
        """TaskLifecycle must not contain reward logic in executable code."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__).lower()
        self.assertNotIn("reward", code)
        self.assertNotIn("balance", code)

    def test_no_referral_logic(self):
        """TaskLifecycle must not contain referral logic in executable code."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__).lower()
        self.assertNotIn("referral", code)

    def test_no_anti_fraud_logic(self):
        """TaskLifecycle must not contain anti-fraud logic in executable code."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__).lower()
        self.assertNotIn("fraud", code)
        self.assertNotIn("cooldown", code)

    def test_no_telegram_logic(self):
        """TaskLifecycle must not contain Telegram logic in executable code."""
        import task_lifecycle as tl_mod
        code = _get_code_only(tl_mod.__file__).lower()
        self.assertNotIn("telegram", code)
        self.assertNotIn("bot.send", code)

    def test_existing_security_boundaries_active(self):
        """Security boundaries from approved modules remain active."""
        # FORBIDDEN_FIELDS is enforced by TaskSubmissionService
        from task_submission import FORBIDDEN_FIELDS
        self.assertIn("reward", FORBIDDEN_FIELDS)
        self.assertIn("status", FORBIDDEN_FIELDS)
        self.assertIn("task_id", FORBIDDEN_FIELDS)
        self.assertIn("user_id", FORBIDDEN_FIELDS)

    def test_freeze_value_exists(self):
        """freeze_value() from task_verifier remains available."""
        from task_verifier import freeze_value, FrozenDict, FrozenList
        frozen = freeze_value({"key": [1, 2, 3]})
        self.assertIsInstance(frozen, FrozenDict)
        self.assertIsInstance(frozen["key"], FrozenList)

    def test_verification_context_is_frozen(self):
        """VerificationContext is a frozen dataclass."""
        from task_verifier import VerificationContext, FrozenDict
        ctx = VerificationContext(
            user_id=100,
            task_id=1,
            task_type="deterministic",
            expected_data=FrozenDict({"expected": "test"}),
            actual_data=FrozenDict({"actual": "test"}),
            task_data=FrozenDict({}),
        )
        # Should raise on attribute assignment (frozen dataclass)
        with self.assertRaises(AttributeError):
            ctx.user_id = 999


if __name__ == "__main__":
    unittest.main()
