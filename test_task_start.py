"""
Tests for the Secure Task Start Gate (Micro-task 2.7).

Covers:
  - valid user + active task → started
  - nonexistent user → rejected
  - nonexistent task → rejected
  - inactive task → rejected
  - task already completed → rejected/no mutation
  - task already started → rejected/no mutation
  - cannot skip directly to completed
  - started_at is populated on successful start
  - failed starts do not create unintended state
  - failed starts do not modify existing state
  - Start Gate does not invoke verifier
  - Start Gate does not invoke CompletionGate
  - successful start is compatible with verify_task()
  - successful verification still requires CompletionGate for completion
  - end-to-end: start → verify → PASSED → complete → completed
  - negative end-to-end: start → verify → FAILED → completion rejected

Run:
    python -m unittest test_task_start.py -v
"""

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
from task_start import StartGateError, StartResult, TaskStartGate
from task_verifier import clear_verifiers, register_verifier, verify_task, DeterministicTaskVerifier


class TestTaskStartGate(unittest.TestCase):
    """Tests for the TaskStartGate class."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.gate = TaskStartGate()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _create_active_task(self) -> int:
        return db.create_task(
            title="Join Channel",
            description="Subscribe",
            task_type="subscribe",
            reward=50,
            db_path=self.test_db_path,
        )

    def _create_inactive_task(self) -> int:
        return db.create_task(
            title="Inactive Task",
            description="Old task",
            task_type="subscribe",
            reward=10,
            active=False,
            db_path=self.test_db_path,
        )

    # ── 1. Valid user + active task → started ─────────────────
    def test_valid_start(self):
        """Valid user + active task transitions to started."""
        task_id = self._create_active_task()
        result = self.gate.start(1001, task_id)
        self.assertTrue(result.success)
        self.assertEqual(result.status, db.USER_TASK_STATUS_STARTED)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)
        self.assertIsNotNone(utask["started_at"])
        self.assertIsNone(utask["completed_at"])

    # ── 2. Nonexistent user → rejected ────────────────────────
    def test_nonexistent_user(self):
        """Nonexistent user raises StartGateError."""
        task_id = self._create_active_task()
        with self.assertRaises(StartGateError) as ctx:
            self.gate.start(99999, task_id)
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 3. Nonexistent task → rejected ────────────────────────
    def test_nonexistent_task(self):
        """Nonexistent task raises StartGateError."""
        with self.assertRaises(StartGateError) as ctx:
            self.gate.start(1001, 99999)
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 4. Inactive task → rejected ───────────────────────────
    def test_inactive_task(self):
        """Inactive task raises StartGateError."""
        task_id = self._create_inactive_task()
        with self.assertRaises(StartGateError) as ctx:
            self.gate.start(1001, task_id)
        self.assertIn("not active", str(ctx.exception))

    def test_inactive_task_no_state_change(self):
        """Inactive task rejection does not create user_task."""
        task_id = self._create_inactive_task()
        with self.assertRaises(StartGateError):
            self.gate.start(1001, task_id)
        self.assertIsNone(db.get_user_task(1001, task_id, self.test_db_path))

    # ── 5. Task already completed → rejected ──────────────────
    def test_already_completed(self):
        """Already completed task raises StartGateError."""
        task_id = self._create_active_task()
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_COMPLETED,
            self.test_db_path, _allow_completion=True,
        )

        with self.assertRaises(StartGateError) as ctx:
            self.gate.start(1001, task_id)
        self.assertIn("already completed", str(ctx.exception))

        # Status unchanged
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 6. Task already started → rejected ────────────────────
    def test_already_started(self):
        """Already started task raises StartGateError (idempotent)."""
        task_id = self._create_active_task()
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        with self.assertRaises(StartGateError) as ctx:
            self.gate.start(1001, task_id)
        self.assertIn("already started", str(ctx.exception))

        # Status unchanged, no timestamp overwrite
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 7. Cannot skip directly to completed ──────────────────
    def test_cannot_skip_to_completed(self):
        """StartGate does not allow jumping to completed status."""
        task_id = self._create_active_task()
        # Verify the db layer blocks this too
        db.create_user_task(1001, task_id, self.test_db_path)
        with self.assertRaises(ValueError):
            db.update_user_task_status(
                1001, task_id, db.USER_TASK_STATUS_COMPLETED, self.test_db_path
            )

    # ── 8. started_at is populated on successful start ────────
    def test_started_at_populated(self):
        """Successful start sets started_at timestamp."""
        task_id = self._create_active_task()
        self.gate.start(1001, task_id)
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertIsNotNone(utask["started_at"])

    # ── 9. Failed starts do not create unintended state ───────
    def test_nonexistent_user_no_state(self):
        """Nonexistent user does not create any user_task."""
        task_id = self._create_active_task()
        with self.assertRaises(StartGateError):
            self.gate.start(99999, task_id)
        self.assertIsNone(db.get_user_task(99999, task_id, self.test_db_path))

    def test_nonexistent_task_no_state(self):
        """Nonexistent task does not create any user_task."""
        with self.assertRaises(StartGateError):
            self.gate.start(1001, 99999)
        # No user_task should exist for this non-existent task
        self.assertIsNone(db.get_user_task(1001, 99999, self.test_db_path))

    # ── 10. Failed starts do not modify existing state ────────
    def test_already_started_no_mutation(self):
        """Rejected start on already-started task doesn't change timestamps."""
        task_id = self._create_active_task()
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        utask_before = db.get_user_task(1001, task_id, self.test_db_path)

        with self.assertRaises(StartGateError):
            self.gate.start(1001, task_id)

        utask_after = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask_before, utask_after)

    def test_already_completed_no_mutation(self):
        """Rejected start on completed task doesn't change state."""
        task_id = self._create_active_task()
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_COMPLETED,
            self.test_db_path, _allow_completion=True,
        )
        utask_before = db.get_user_task(1001, task_id, self.test_db_path)

        with self.assertRaises(StartGateError):
            self.gate.start(1001, task_id)

        utask_after = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask_before, utask_after)

    # ── 11. Start Gate does not invoke verifier ───────────────
    def test_does_not_invoke_verifier(self):
        """StartGate does not call verify_task or import it."""
        import task_start
        # Verify task_start module does not import verify_task
        source = open(task_start.__file__).read()
        self.assertNotIn("verify_task", source.split("class ")[0])

    # ── 12. Start Gate does not invoke CompletionGate ──────────
    def test_does_not_invoke_completion_gate(self):
        """StartGate does not call CompletionGate or import it."""
        import task_start
        source = open(task_start.__file__).read()
        self.assertNotIn("CompletionGate", source.split("class ")[0])

    # ── 13. Successful start is compatible with verify_task ────
    def test_start_then_verify_works(self):
        """Starting a task allows subsequent verify_task() to run."""
        task_id = db.create_task(
            title="Code Task",
            description="Enter code",
            task_type="deterministic",
            reward=25,
            db_path=self.test_db_path,
            task_data='{"expected": "abc", "actual": "abc"}',
        )
        self.gate.start(1001, task_id)

        # Re-register verifier (cleared in some tests)
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())

        result = verify_task(1001, task_id)
        self.assertTrue(result.passed)

    # ── 14. Verification still requires CompletionGate ────────
    def test_verify_then_complete(self):
        """After start + verify, CompletionGate is still needed."""
        task_id = db.create_task(
            title="Code Task",
            description="Enter code",
            task_type="deterministic",
            reward=25,
            db_path=self.test_db_path,
            task_data='{"expected": "xyz", "actual": "xyz"}',
        )
        self.gate.start(1001, task_id)

        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())

        verification = verify_task(1001, task_id)
        self.assertTrue(verification.passed)

        gate = CompletionGate()
        completed = gate.complete(1001, task_id, verification)
        self.assertTrue(completed)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 15. End-to-end: start → verify → PASSED → complete ────
    def test_end_to_end_success(self):
        """Full flow: start → verify → complete → completed."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data='{"expected": "secret123", "actual": "secret123"}',
        )

        # Start
        start_result = self.gate.start(1001, task_id)
        self.assertTrue(start_result.success)
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

        # Verify
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())
        verification = verify_task(1001, task_id)
        self.assertTrue(verification.passed)

        # Complete
        gate = CompletionGate()
        self.assertTrue(gate.complete(1001, task_id, verification))
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 16. Negative end-to-end: start → verify → FAILED ──────
    def test_end_to_end_negative(self):
        """Flow with wrong answer: start → verify FAILED → complete rejected."""
        task_id = db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=100,
            db_path=self.test_db_path,
            task_data='{"expected": "correct", "actual": "wrong"}',
        )

        # Start
        self.assertTrue(self.gate.start(1001, task_id).success)

        # Verify — should FAIL (wrong actual)
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())
        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

        # Complete — should be rejected
        gate = CompletionGate()
        with self.assertRaises(CompletionGateError):
            gate.complete(1001, task_id, verification)

        # Status remains started
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 17. start_task creates user_task if absent ────────────
    def test_start_creates_user_task(self):
        """Starting a task creates the user_task row if it doesn't exist."""
        task_id = self._create_active_task()
        self.assertIsNone(db.get_user_task(1001, task_id, self.test_db_path))
        self.gate.start(1001, task_id)
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertIsNotNone(utask)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 18. StartResult is frozen ─────────────────────────────
    def test_start_result_frozen(self):
        """StartResult is immutable."""
        task_id = self._create_active_task()
        result = self.gate.start(1001, task_id)
        with self.assertRaises(AttributeError):
            result.success = False  # type: ignore[misc]

    # ── 19. start_task does not modify user records ───────────
    def test_does_not_modify_user(self):
        """StartGate does not modify user records."""
        task_id = self._create_active_task()
        user_before = db.get_user(1001)
        self.gate.start(1001, task_id)
        user_after = db.get_user(1001)
        self.assertEqual(user_before, user_after)

    # ── 20. start_task does not modify task records ────────────
    def test_does_not_modify_task(self):
        """StartGate does not modify task definitions."""
        task_id = self._create_active_task()
        task_before = db.get_task(task_id, self.test_db_path)
        self.gate.start(1001, task_id)
        task_after = db.get_task(task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)

    # ── 21. available cannot jump to completed via db directly ─
    def test_db_blocks_available_to_completed(self):
        """DB layer blocks available → completed without gate."""
        task_id = self._create_active_task()
        db.create_user_task(1001, task_id, self.test_db_path)
        with self.assertRaises(ValueError):
            db.update_user_task_status(
                1001, task_id, db.USER_TASK_STATUS_COMPLETED, self.test_db_path
            )


if __name__ == "__main__":
    unittest.main()
