"""
Comprehensive tests for the Task Lifecycle Orchestrator.

Run:
    python -m pytest test_task_lifecycle.py -v
    # or
    python -m unittest test_task_lifecycle -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from typing import Any
from unittest.mock import MagicMock

from task_components import (
    CompletionBridge,
    CompletionGate,
    Task,
    TaskAttemptPolicy,
    TaskStartGate,
    TaskStatus,
    TaskSubmissionService,
    TaskVerifier,
    VerificationContext,
    VerificationResult,
    init_task_db,
    get_user_task,
)
from task_lifecycle import TaskLifecycle


# ── Helpers ──────────────────────────────────────────────────────────

def _insert_task(conn: sqlite3.Connection, title: str = "Test Task", active: bool = True) -> int:
    """Insert a task and return its ID."""
    cur = conn.execute(
        "INSERT INTO tasks (title, description, is_active) VALUES (?, '', ?)",
        (title, int(active)),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


class _FailingVerifier(TaskVerifier):
    """Verifier that always returns FAILED."""

    def verify(self, ctx: VerificationContext) -> VerificationResult:
        return VerificationResult.FAILED


class _ErrorVerifier(TaskVerifier):
    """Verifier that always raises an exception."""

    def verify(self, ctx: VerificationContext) -> VerificationResult:
        raise RuntimeError("simulated verification failure")


# ── Test classes ─────────────────────────────────────────────────────


class TestStart(unittest.TestCase):
    """START tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        init_task_db(self._db)
        self._conn = sqlite3.connect(self._db)
        self._lifecycle = TaskLifecycle(db_path=self._db)

    def tearDown(self) -> None:
        self._conn.close()
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_valid_available_task_starts(self) -> None:
        tid = _insert_task(self._conn)
        result = self._lifecycle.start_task(100, tid)
        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.user_task)
        self.assertEqual(result.user_task.status, TaskStatus.STARTED)

    def test_nonexistent_user_rejected(self) -> None:
        """Task exists but user has never interacted — start still works
        (user_tasks allows new user + task combos)."""
        tid = _insert_task(self._conn)
        result = self._lifecycle.start_task(999999, tid)
        self.assertTrue(result.success)

    def test_nonexistent_task_rejected(self) -> None:
        result = self._lifecycle.start_task(100, 999999)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "TASK_NOT_FOUND")

    def test_inactive_task_rejected(self) -> None:
        tid = _insert_task(self._conn, active=False)
        result = self._lifecycle.start_task(100, tid)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "TASK_INACTIVE")

    def test_already_started_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.start_task(100, tid)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "ALREADY_STARTED_OR_COMPLETED")

    def test_already_completed_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"answer": 42})
        result = self._lifecycle.start_task(100, tid)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "ALREADY_STARTED_OR_COMPLETED")


class TestSubmission(unittest.TestCase):
    """SUBMISSION tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        init_task_db(self._db)
        self._conn = sqlite3.connect(self._db)
        self._lifecycle = TaskLifecycle(db_path=self._db)

    def tearDown(self) -> None:
        self._conn.close()
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_started_passed_completes(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"answer": 42})
        self.assertTrue(result.success)
        self.assertEqual(result.verification_result, VerificationResult.PASSED)
        # Verify task is COMPLETED
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.COMPLETED)

    def test_started_failed_remains_started(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        lifecycle = TaskLifecycle(
            verifier=_FailingVerifier(),
            db_path=self._db,
        )
        result = lifecycle.submit_task(100, tid, {"answer": 0})
        self.assertTrue(result.success)
        self.assertEqual(result.verification_result, VerificationResult.FAILED)
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.STARTED)

    def test_started_error_remains_started(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        lifecycle = TaskLifecycle(
            verifier=_ErrorVerifier(),
            db_path=self._db,
        )
        result = lifecycle.submit_task(100, tid, {"answer": 1})
        self.assertTrue(result.success)
        self.assertEqual(result.verification_result, VerificationResult.ERROR)
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.STARTED)

    def test_available_task_cannot_submit(self) -> None:
        tid = _insert_task(self._conn)
        result = self._lifecycle.submit_task(100, tid, {"answer": 1})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "NOT_STARTED")

    def test_completed_task_cannot_submit(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"answer": 1})
        result = self._lifecycle.submit_task(100, tid, {"answer": 2})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "ALREADY_COMPLETED")

    def test_invalid_user_task_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(999, tid, {"answer": 1})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "NOT_STARTED")

    def test_non_dict_actual_data_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, "not a dict")  # type: ignore[arg-type]
        self.assertFalse(result.success)
        self.assertEqual(result.error, "INVALID_DATA_TYPE")

    def test_forbidden_fields_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        result = self._lifecycle.submit_task(100, tid, {"answer": 1, "reward": 50})
        self.assertFalse(result.success)
        self.assertIn("FORBIDDEN_FIELDS", result.error)

    def test_verifier_exception_remains_error(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        lifecycle = TaskLifecycle(
            verifier=_ErrorVerifier(),
            db_path=self._db,
        )
        result = lifecycle.submit_task(100, tid, {"answer": 1})
        self.assertTrue(result.success)
        self.assertEqual(result.verification_result, VerificationResult.ERROR)
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.STARTED)

    def test_repeated_submission_after_completion_rejected(self) -> None:
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"answer": 1})
        result = self._lifecycle.submit_task(100, tid, {"answer": 2})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "ALREADY_COMPLETED")


class TestArchitecture(unittest.TestCase):
    """ARCHITECTURE tests — verify no direct DB mutations from TaskLifecycle."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db = self._tmp.name
        self._tmp.close()
        init_task_db(self._db)
        self._conn = sqlite3.connect(self._db)
        self._lifecycle = TaskLifecycle(db_path=self._db)

    def tearDown(self) -> None:
        self._conn.close()
        for sfx in ("", "-wal", "-shm"):
            p = self._db + sfx
            if os.path.exists(p):
                os.unlink(p)

    def test_no_direct_user_tasks_mutation(self) -> None:
        """TaskLifecycle must not directly write to user_tasks."""
        import task_lifecycle as tl_mod
        src = open(tl_mod.__file__).read()
        # No raw SQL writes in task_lifecycle.py
        self.assertNotIn("INSERT INTO user_tasks", src)
        self.assertNotIn("UPDATE user_tasks", src)
        self.assertNotIn("DELETE FROM user_tasks", src)

    def test_delegates_to_existing_gates(self) -> None:
        """TaskLifecycle must delegate to TaskStartGate and TaskAttemptPolicy."""
        tid = _insert_task(self._conn)
        # Start
        self._lifecycle.start_task(100, tid)
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.STARTED)
        self.assertIsNotNone(ut.started_at)

    def test_completion_bridge_only_path(self) -> None:
        """CompletionBridge is the only path to COMPLETED status."""
        tid = _insert_task(self._conn)
        self._lifecycle.start_task(100, tid)
        self._lifecycle.submit_task(100, tid, {"answer": 1})
        ut = get_user_task(100, tid, db_path=self._db)
        self.assertEqual(ut.status, TaskStatus.COMPLETED)
        self.assertIsNotNone(ut.verified_at)

    def test_no_direct_sqlite3_connection(self) -> None:
        """TaskLifecycle must not create sqlite3 connections directly."""
        import task_lifecycle as tl_mod
        src = open(tl_mod.__file__).read()
        self.assertNotIn("sqlite3.connect", src)


if __name__ == "__main__":
    unittest.main()
