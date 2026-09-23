"""
Tests for the Secure Task Completion Gate.

Run:
    python -m pytest test_task_completion.py -v
    # or
    python -m unittest test_task_completion.py -v
"""

import os
import tempfile
import threading
import unittest

from config import CHANNELS
import db
from completion_bridge import CompletionBridge
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)
from task_verifier import TaskVerifier, clear_verifiers, register_verifier


class TestVerificationContract(unittest.TestCase):
    """Tests for the VerificationResult dataclass."""

    def test_passed_property_true(self):
        """VerificationResult with PASSED status reports passed=True."""
        v = VerificationResult(status=VerificationStatus.PASSED)
        self.assertTrue(v.passed)

    def test_passed_property_false_for_failed(self):
        """VerificationResult with FAILED status reports passed=False."""
        v = VerificationResult(status=VerificationStatus.FAILED, reason="not subscribed")
        self.assertFalse(v.passed)

    def test_passed_property_false_for_error(self):
        """VerificationResult with ERROR status reports passed=False."""
        v = VerificationResult(status=VerificationStatus.ERROR, reason="api timeout")
        self.assertFalse(v.passed)

    def test_default_reason_is_empty(self):
        """Default reason is an empty string."""
        v = VerificationResult(status=VerificationStatus.PASSED)
        self.assertEqual(v.reason, "")

    def test_frozen_dataclass(self):
        """VerificationResult is immutable."""
        v = VerificationResult(status=VerificationStatus.PASSED)
        with self.assertRaises(AttributeError):
            v.status = VerificationStatus.FAILED  # type: ignore[misc]


class TestCompletionGate(unittest.TestCase):
    """Tests for the CompletionGate class."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        # Register a user and create a task
        db.register_user(1001, "alice", "Alice")
        self.task_id = db.create_task(
            title="Join Channel",
            description="Subscribe",
            task_type="subscribe",
            reward=50,
            db_path=self.test_db_path,
        )
        self.gate = CompletionGate()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _start_task(self):
        """Helper: create user_task and transition to started."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

    # ── 1. Verified completion succeeds ────────────────────────────
    def test_verified_completion_succeeds(self):
        """Completion with passed verification succeeds."""
        self._start_task()
        result = self.gate.complete(
            1001, self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        self.assertTrue(result)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(row["completed_at"])

    # ── 2. Completion without verification fails ───────────────────
    def test_completion_without_verification_fails(self):
        """Completion with failed verification raises CompletionGateError."""
        self._start_task()
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1001, self.task_id,
                VerificationResult(status=VerificationStatus.FAILED, reason="not subscribed"),
            )
        self.assertIn("Verification failed", str(ctx.exception))
        # Status must remain started
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)

    # ── 3. Completed task cannot be completed again ────────────────
    def test_completed_task_rejected_on_second_attempt(self):
        """A completed task cannot be completed again (idempotent guard)."""
        self._start_task()
        self.gate.complete(
            1001, self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1001, self.task_id,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        self.assertIn("completed", str(ctx.exception))

    # ── 4. available cannot jump to completed via gate ──────────────
    def test_available_cannot_jump_to_completed(self):
        """available → completed is rejected even through the gate."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1001, self.task_id,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        self.assertIn("started", str(ctx.exception))

    # ── 5. Non-existent user is rejected ───────────────────────────
    def test_nonexistent_user_rejected(self):
        """Completion is rejected for a user that does not exist."""
        self._start_task()
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                99999, self.task_id,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 6. Non-existent task is rejected ───────────────────────────
    def test_nonexistent_task_rejected(self):
        """Completion is rejected for a task that does not exist."""
        self._start_task()
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1001, 99999,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("not found", str(ctx.exception))

    # ── 7. Non-existent user_task is rejected ──────────────────────
    def test_nonexistent_user_task_rejected(self):
        """Completion is rejected when no user_task row exists."""
        db.register_user(1002, "bob", "Bob")
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1002, self.task_id,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        self.assertIn("No user_task record", str(ctx.exception))

    # ── 8. Idempotency — completing same task twice is rejected ─────
    def test_idempotency_rejects_double_completion(self):
        """Second completion attempt on same task raises error."""
        self._start_task()
        self.gate.complete(
            1001, self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        with self.assertRaises(CompletionGateError):
            self.gate.complete(
                1001, self.task_id,
                VerificationResult(status=VerificationStatus.PASSED),
            )
        # Verify status is still completed (unchanged)
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)

    # ── 9. No reward is granted ────────────────────────────────────
    def test_no_reward_granted(self):
        """Completion does not create any reward records."""
        self._start_task()
        # Verify no reward tables exist (our schema has no rewards table)
        self.gate.complete(
            1001, self.task_id,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        # Confirm no reward-related side effects: user record unchanged
        user = db.get_user(1001)
        self.assertIsNotNone(user)
        self.assertEqual(user["user_id"], 1001)
        # Confirm tasks table reward field unchanged
        task = db.get_task(self.task_id, self.test_db_path)
        self.assertEqual(task["reward"], 50)

    # ── 10. No referral state modification ─────────────────────────
    def test_no_referral_state_modified(self):
        """Completion does not modify referral attribution."""
        # Set up referral chain
        db.register_user(1003, "referrer", "Referrer")
        db.register_user(1004, "referred", "Referred", referred_by=1003)

        # Create and start a task for the referred user
        task_id2 = db.create_task(
            title="Visit Site",
            description="Go to site",
            task_type="visit",
            reward=10,
            db_path=self.test_db_path,
        )
        db.create_user_task(1004, task_id2, self.test_db_path)
        db.update_user_task_status(
            1004, task_id2, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Complete the task
        self.gate.complete(
            1004, task_id2,
            VerificationResult(status=VerificationStatus.PASSED),
        )

        # Verify referral state is unchanged
        user = db.get_user(1004)
        self.assertEqual(user["referred_by"], 1003)

        referrer = db.get_user(1003)
        self.assertIsNotNone(referrer)
        self.assertEqual(referrer["referred_by"], None)

        # Referral count unchanged
        self.assertEqual(db.get_referral_count(1003), 1)

    # ── 11. Verification with ERROR status also rejected ───────────
    def test_error_status_rejected(self):
        """Completion with ERROR verification status is rejected."""
        self._start_task()
        with self.assertRaises(CompletionGateError) as ctx:
            self.gate.complete(
                1001, self.task_id,
                VerificationResult(status=VerificationStatus.ERROR, reason="api timeout"),
            )
        self.assertIn("Verification failed", str(ctx.exception))
        self.assertIn("error", str(ctx.exception))

    # ── 12. started → completed blocked without _allow_completion ───
    def test_db_blocks_started_to_completed_without_gate(self):
        """db.update_user_task_status blocks started→completed without _allow_completion."""
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        with self.assertRaises(ValueError) as ctx:
            db.update_user_task_status(
                1001, self.task_id, db.USER_TASK_STATUS_COMPLETED, self.test_db_path
            )
        self.assertIn("CompletionGate", str(ctx.exception))


class _AlwaysPassVerifier(TaskVerifier):
    """Test verifier: always PASSED (registered only inside tests)."""

    def verify(self, context) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.PASSED)


class TestConcurrentCompletion(unittest.TestCase):
    """Concurrency: simultaneous completions transition exactly once.

    The CompletionGate validates and transitions inside db.transaction()
    (BEGIN IMMEDIATE), so racing successful attempts serialize: exactly
    one moves started → completed, the others receive the existing
    failure/error behavior — never a second completion.
    """

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
            description="Subscribe",
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

    def _start_task(self) -> None:
        db.create_user_task(1001, self.task_id, self.test_db_path)
        db.update_user_task_status(
            1001, self.task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

    def test_two_simultaneous_completions_exactly_one_wins(self):
        """Two racing CompletionGate calls: one wins, one is rejected."""
        self._start_task()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        messages: list[str] = []

        def attempt() -> None:
            barrier.wait()
            try:
                ok = CompletionGate().complete(
                    1001, self.task_id,
                    VerificationResult(status=VerificationStatus.PASSED),
                )
                outcomes.append("success" if ok else "rejected")
            except CompletionGateError as exc:
                outcomes.append("rejected")
                messages.append(str(exc))

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(outcomes), 2, f"outcomes={outcomes}")
        self.assertEqual(outcomes.count("success"), 1, f"outcomes={outcomes}")
        self.assertEqual(outcomes.count("rejected"), 1, f"outcomes={outcomes}")
        # Loser got the existing rejection vocabulary (terminal state)
        self.assertIn("completed", messages[0])
        # Final state correct, single completion, no corruption
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(row["completed_at"])

    def test_two_simultaneous_submissions_single_completion(self):
        """Two racing full submissions: one PASSED, one non-passed."""
        self._start_task()
        register_verifier("subscribe", _AlwaysPassVerifier())
        barrier = threading.Barrier(2)
        results: list[VerificationResult] = []

        def attempt() -> None:
            barrier.wait()
            results.append(
                CompletionBridge.complete_after_verification(
                    1001, self.task_id, {"actual": "x"}
                )
            )

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Both attempts produced a VerificationResult (never a crash)
        self.assertEqual(len(results), 2, f"results={results}")
        passed = [r for r in results if r.status == VerificationStatus.PASSED]
        self.assertEqual(len(passed), 1, f"results={results}")
        # The loser received existing failure/error behavior
        losers = [r for r in results if r.status != VerificationStatus.PASSED]
        self.assertEqual(len(losers), 1)
        self.assertIn(losers[0].status,
                      (VerificationStatus.FAILED, VerificationStatus.ERROR))
        # Exactly one completion; final state correct
        row = db.get_user_task(1001, self.task_id, self.test_db_path)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(row["completed_at"])


if __name__ == "__main__":
    unittest.main()
