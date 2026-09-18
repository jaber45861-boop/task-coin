"""
Tests for the Deterministic Task Verifier (Micro-task 2.6).

Covers:
  - Exact expected value → PASSED
  - Incorrect value → FAILED
  - Missing verification value → ERROR
  - Missing expected value → ERROR
  - Malformed task_data → ERROR
  - Extra unrelated task_data does not bypass verification
  - Verifier cannot complete a task by itself
  - Successful VerificationResult → CompletionGate works
  - Failed VerificationResult → CompletionGate rejects
  - Verifier does not mutate database state
  - End-to-end: create/start → verify → PASSED → complete → completed
  - Negative path: create/start → verify → FAILED → complete rejected → remains started

Run:
    python -m unittest test_deterministic_verifier -v
"""

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
from task_verifier import (
    DeterministicTaskVerifier,
    VerificationContext,
    clear_verifiers,
    get_verifier,
    register_verifier,
    verify_task,
)


class TestDeterministicVerifierUnit(unittest.TestCase):
    """Unit tests for DeterministicTaskVerifier.verify() directly."""

    def setUp(self):
        self.verifier = DeterministicTaskVerifier()

    # ── 1. Exact expected value → PASSED ──────────────────────
    def test_exact_match_passed(self):
        """Matching expected/actual returns PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "hello", "actual": "hello"},
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)
        self.assertEqual(result.reason, "")

    def test_exact_match_numeric(self):
        """Numeric exact match returns PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": 42, "actual": 42},
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    def test_exact_match_boolean(self):
        """Boolean exact match returns PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": True, "actual": True},
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    def test_exact_match_empty_string(self):
        """Empty string match returns PASSED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "", "actual": ""},
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)

    # ── 2. Incorrect value → FAILED ───────────────────────────
    def test_mismatch_failed(self):
        """Mismatched expected/actual returns FAILED."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "hello", "actual": "world"},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)
        self.assertIn("expected", result.reason)
        self.assertIn("hello", result.reason)
        self.assertIn("world", result.reason)

    def test_type_mismatch_failed(self):
        """Different types return FAILED (no truthy coercion)."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "1", "actual": 1},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_truthy_does_not_bypass(self):
        """Truthy value does not match expected string."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "yes", "actual": True},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_one_matches_true_in_python(self):
        """In Python, True == 1 because bool is a subclass of int.
        This is expected Python behavior, not a bypass."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": True, "actual": 1},
        )
        result = self.verifier.verify(ctx)
        # Python considers True == 1 as equal — exact match via ==
        self.assertTrue(result.passed)
        self.assertEqual(result.status, VerificationStatus.PASSED)

    # ── 3. Missing 'actual' → ERROR ───────────────────────────
    def test_missing_actual_error(self):
        """Missing 'actual' key returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"expected": "hello"},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("actual", result.reason)

    # ── 4. Missing 'expected' → ERROR ─────────────────────────
    def test_missing_expected_error(self):
        """Missing 'expected' key returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={"actual": "hello"},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("expected", result.reason)

    def test_empty_task_data_error(self):
        """Empty task_data returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={},
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    # ── 5. Malformed task_data → ERROR ────────────────────────
    def test_non_dict_task_data_error(self):
        """Non-dict task_data returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data="not a dict",  # type: ignore[assignment]
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)
        self.assertIn("not a dict", result.reason)

    def test_list_task_data_error(self):
        """List task_data returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=["expected", "actual"],  # type: ignore[assignment]
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    def test_none_task_data_error(self):
        """None task_data returns ERROR."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data=None,  # type: ignore[assignment]
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.ERROR)

    # ── 6. Extra keys do not bypass verification ──────────────
    def test_extra_keys_do_not_bypass(self):
        """Extra keys in task_data don't affect the expected/actual check."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={
                "expected": "secret_code",
                "actual": "wrong_code",
                "bypass": "anything",
                "admin": True,
                "completed": True,
            },
        )
        result = self.verifier.verify(ctx)
        self.assertFalse(result.passed)
        self.assertEqual(result.status, VerificationStatus.FAILED)

    def test_extra_keys_with_correct_match(self):
        """Correct match with extra keys still passes."""
        ctx = VerificationContext(
            user_id=1, task_id=1, task_type="deterministic",
            task_data={
                "expected": "secret_code",
                "actual": "secret_code",
                "extra": "ignored",
            },
        )
        result = self.verifier.verify(ctx)
        self.assertTrue(result.passed)


class TestDeterministicVerifierRegistry(unittest.TestCase):
    """Test that DeterministicTaskVerifier is registered correctly."""

    def setUp(self):
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())

    def tearDown(self):
        clear_verifiers()

    def test_registered_for_deterministic(self):
        """DeterministicTaskVerifier is registered for 'deterministic' type."""
        verifier = get_verifier("deterministic")
        self.assertIsNotNone(verifier)
        self.assertIsInstance(verifier, DeterministicTaskVerifier)

    def test_is_task_verifier_subclass(self):
        """DeterministicTaskVerifier is a proper TaskVerifier subclass."""
        from task_verifier import TaskVerifier
        self.assertTrue(issubclass(DeterministicTaskVerifier, TaskVerifier))


class TestDeterministicVerifierIntegration(unittest.TestCase):
    """Integration tests with database and CompletionGate."""

    def setUp(self):
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(self.test_db_path)
        db.register_user(1001, "alice", "Alice")
        self.gate = CompletionGate()
        # Re-register after any prior tearDown clear_verifiers()
        clear_verifiers()
        register_verifier("deterministic", DeterministicTaskVerifier())

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

    def _create_deterministic_task(self, expected_value: str) -> int:
        """Helper: create a deterministic task with the given expected value."""
        return db.create_task(
            title="Enter Code",
            description="Enter the correct code",
            task_type="deterministic",
            reward=25,
            db_path=self.test_db_path,
            task_data=json.dumps({"expected": expected_value}),
        )

    # ── 7. Verifier cannot complete a task by itself ──────────
    def test_verifier_does_not_complete_task(self):
        """verify_task returns VerificationResult but does not transition status."""
        task_id = self._create_deterministic_task("ABC123")
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Manually inject 'actual' into task_data via the context
        # We need to call verify_task but it reads task_data from DB
        # So we set the task_data with both expected and actual
        db.update_task(
            task_id,
            db_path=self.test_db_path,
        )
        # Override task_data to include actual
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "ABC123", "actual": "ABC123"}), task_id),
            )

        result = verify_task(1001, task_id)
        self.assertTrue(result.passed)

        # Status should still be 'started'
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 8. Successful VerificationResult → CompletionGate ─────
    def test_passed_verification_completes_via_gate(self):
        """PASSED verification feeds into CompletionGate → completed."""
        task_id = self._create_deterministic_task("SECRET")
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with correct actual value
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "SECRET", "actual": "SECRET"}), task_id),
            )

        verification = verify_task(1001, task_id)
        self.assertTrue(verification.passed)

        completed = self.gate.complete(1001, task_id, verification)
        self.assertTrue(completed)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(utask["completed_at"])

    # ── 9. Failed VerificationResult → CompletionGate rejects ─
    def test_failed_verification_rejected_by_gate(self):
        """FAILED verification is rejected by CompletionGate."""
        task_id = self._create_deterministic_task("CORRECT")
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with wrong actual value
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "CORRECT", "actual": "WRONG"}), task_id),
            )

        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

        with self.assertRaises(CompletionGateError):
            self.gate.complete(1001, task_id, verification)

        # Status remains started
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    def test_error_verification_rejected_by_gate(self):
        """ERROR verification is rejected by CompletionGate."""
        task_id = self._create_deterministic_task("CORRECT")
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with missing 'actual' (will trigger ERROR)
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "CORRECT"}), task_id),
            )

        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.ERROR)

        with self.assertRaises(CompletionGateError):
            self.gate.complete(1001, task_id, verification)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 10. Verifier does not mutate database state ───────────
    def test_verifier_does_not_mutate_users(self):
        """Verifier does not modify user records."""
        task_id = self._create_deterministic_task("X")
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "X", "actual": "X"}), task_id),
            )

        user_before = db.get_user(1001)
        verify_task(1001, task_id)
        user_after = db.get_user(1001)
        self.assertEqual(user_before, user_after)

    def test_verifier_does_not_mutate_task(self):
        """Verifier does not modify task definitions."""
        task_id = self._create_deterministic_task("Y")
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "Y", "actual": "Y"}), task_id),
            )

        task_before = db.get_task(task_id, self.test_db_path)
        verify_task(1001, task_id)
        task_after = db.get_task(task_id, self.test_db_path)
        self.assertEqual(task_before, task_after)

    def test_verifier_does_not_mutate_user_task(self):
        """Verifier does not modify user_tasks records."""
        task_id = self._create_deterministic_task("Z")
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "Z", "actual": "Z"}), task_id),
            )

        utask_before = db.get_user_task(1001, task_id, self.test_db_path)
        verify_task(1001, task_id)
        utask_after = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask_before, utask_after)

    # ── 11. End-to-end: full success flow ─────────────────────
    def test_end_to_end_success(self):
        """Full flow: create task → start → verify (PASSED) → complete → completed."""
        task_id = self._create_deterministic_task("CORRECT_ANSWER")

        # Create user_task and start it
        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with correct actual value
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "CORRECT_ANSWER", "actual": "CORRECT_ANSWER"}), task_id),
            )

        # Verify
        verification = verify_task(1001, task_id)
        self.assertTrue(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.PASSED)

        # Complete
        result = self.gate.complete(1001, task_id, verification)
        self.assertTrue(result)

        # Assert final state
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNotNone(utask["completed_at"])

    # ── 12. Negative path: full failure flow ──────────────────
    def test_end_to_end_wrong_answer_rejected(self):
        """Full flow: create → start → verify (FAILED) → complete rejected → remains started."""
        task_id = self._create_deterministic_task("CORRECT")

        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with WRONG actual value
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "CORRECT", "actual": "WRONG"}), task_id),
            )

        # Verify — should FAIL
        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

        # Complete — should be rejected
        with self.assertRaises(CompletionGateError):
            self.gate.complete(1001, task_id, verification)

        # Status remains started
        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    def test_end_to_end_missing_data_rejected(self):
        """Full flow: create → start → verify (ERROR) → complete rejected → remains started."""
        task_id = self._create_deterministic_task("SECRET")

        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Set task_data with missing 'actual' key
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({"expected": "SECRET"}), task_id),
            )

        # Verify — should ERROR
        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.ERROR)

        # Complete — should be rejected
        with self.assertRaises(CompletionGateError):
            self.gate.complete(1001, task_id, verification)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 13. task_data stored as JSON in DB ────────────────────
    def test_task_data_persists_as_json(self):
        """task_data is stored as JSON string in DB and parsed back."""
        expected = {"expected": "CODE", "extra": 123}
        task_id = db.create_task(
            title="Code Task",
            description="Enter code",
            task_type="deterministic",
            reward=10,
            db_path=self.test_db_path,
            task_data=json.dumps(expected),
        )

        # Raw DB stores JSON string
        with db.get_connection(self.test_db_path) as conn:
            row = conn.execute(
                "SELECT task_data FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self.assertIsInstance(row["task_data"], str)

        # get_task returns the JSON string
        task = db.get_task(task_id, self.test_db_path)
        self.assertEqual(task["task_data"], json.dumps(expected))

    # ── 14. No task_data defaults to None ─────────────────────
    def test_no_task_data_defaults_to_none(self):
        """Tasks without task_data have task_data=None."""
        task_id = db.create_task(
            title="Plain Task",
            description="No data",
            task_type="subscribe",
            reward=5,
            db_path=self.test_db_path,
        )
        task = db.get_task(task_id, self.test_db_path)
        self.assertIsNone(task["task_data"])

    # ── 15. Client-supplied 'completed' field does not bypass ─
    def test_completed_field_does_not_bypass(self):
        """A client-supplied 'completed=true' does not bypass expected/actual check."""
        task_id = self._create_deterministic_task("REAL_ANSWER")

        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        # Inject malicious task_data with completed=true but wrong actual
        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({
                    "expected": "REAL_ANSWER",
                    "actual": "WRONG",
                    "completed": True,
                    "admin": True,
                }), task_id),
            )

        verification = verify_task(1001, task_id)
        self.assertFalse(verification.passed)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

        with self.assertRaises(CompletionGateError):
            self.gate.complete(1001, task_id, verification)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_STARTED)

    # ── 16. Client-supplied 'completed' with correct actual ───
    def test_completed_with_correct_actual_still_passes(self):
        """Correct actual value passes even if 'completed' key is also present."""
        task_id = self._create_deterministic_task("ANSWER")

        db.create_user_task(1001, task_id, self.test_db_path)
        db.update_user_task_status(
            1001, task_id, db.USER_TASK_STATUS_STARTED, self.test_db_path
        )

        with db.get_connection(self.test_db_path) as conn:
            conn.execute(
                "UPDATE tasks SET task_data = ? WHERE id = ?",
                (json.dumps({
                    "expected": "ANSWER",
                    "actual": "ANSWER",
                    "completed": True,
                }), task_id),
            )

        verification = verify_task(1001, task_id)
        self.assertTrue(verification.passed)

        result = self.gate.complete(1001, task_id, verification)
        self.assertTrue(result)

        utask = db.get_user_task(1001, task_id, self.test_db_path)
        self.assertEqual(utask["status"], db.USER_TASK_STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main()
