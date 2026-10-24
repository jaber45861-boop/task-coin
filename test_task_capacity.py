"""
Task Execution Limits & Reservation Expiry (MT-TASK-28)
=======================================================

Advertiser-set execution capacity, repeat policy and per-start
reservation windows.

Legacy compatibility is the central invariant: a task whose
``max_executions`` is NULL is unlimited and one whose
``reservation_minutes`` is NULL never expires — both the exact
behaviour before these columns existed.  Every test here runs against
a temporary SQLite file and never touches a production wallet, ledger
or reward.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest

import db
from manual_task import ManualProofService
from task_capacity import (
    MAX_EXECUTIONS_CEILING,
    RESERVATION_MINUTES_CEILING,
    CapacityExhaustedError,
    claimed_capacity,
    is_reservation_expired,
    release_expired_reservation,
    reservation_deadline,
    sweep_expired_reservations,
    validate_max_executions,
    validate_reservation_minutes,
)
from task_start import TaskStartGate, StartGateError
from task_completion import (
    CompletionGate,
    VerificationResult,
    VerificationStatus,
)


def _task(**kwargs):
    base = dict(
        title="مهمة",
        description="وصف",
        task_type="manual",
        reward=10,
    )
    base.setdefault(
        "task_data",
        json.dumps({
            "provider": "other",
            "action": "follow",
            "approver": {"telegram_user_id": 1},
            "target": {"ref": "https://example.com/x"},
        }),
    )
    base.update(kwargs)
    return db.create_task(**base)


def _user(uid):
    db.register_user(uid, f"u{uid}", f"U{uid}")


class CapacityTestBase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self._orig = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._orig

    # helpers -------------------------------------------------------

    def _raw(self, sql, *params):
        with db.get_connection() as conn:
            return conn.execute(sql, params).fetchall()

    def _started(self, task_id, users):
        gate = TaskStartGate()
        started = []
        for uid in users:
            _user(uid)
            gate.start(uid, task_id)
            started.append(uid)
        return started

    def _force_expiry(self, task_id, users):
        past = "2000-01-01T00:00:00+00:00"
        with db.get_connection() as conn:
            for uid in users:
                conn.execute(
                    "UPDATE user_tasks SET reserved_until = ? "
                    "WHERE user_id = ? AND task_id = ?",
                    (past, uid, task_id),
                )


# ════════════════════════════════════════════════════════════════════
# 1–2. Field validation (server-side, never trust the client)
# ════════════════════════════════════════════════════════════════════


class TestExecutionLimitValidation(unittest.TestCase):
    """max_executions / reservation_minutes validation."""

    def test_none_means_not_configured(self):
        self.assertIsNone(validate_max_executions(None))
        self.assertIsNone(validate_reservation_minutes(None))

    def test_empty_string_means_not_configured(self):
        self.assertIsNone(validate_max_executions(""))
        self.assertIsNone(validate_reservation_minutes("  "))

    def test_positive_values_accepted(self):
        self.assertEqual(validate_max_executions(100), 100)
        self.assertEqual(validate_max_executions("500"), 500)
        self.assertEqual(validate_reservation_minutes(30), 30)
        self.assertEqual(validate_reservation_minutes("60"), 60)

    def test_zero_rejected(self):
        for bad in (0, "0", 0.0, False):
            with self.assertRaises(ValueError):
                validate_max_executions(bad)
            with self.assertRaises(ValueError):
                validate_reservation_minutes(bad)

    def test_negative_rejected(self):
        for bad in (-1, "-5", -100):
            with self.assertRaises(ValueError):
                validate_max_executions(bad)

    def test_non_numeric_rejected(self):
        for bad in ("abc", "1.5", "1e3", "12,000", {}, [], "10 extra"):
            with self.assertRaises(ValueError):
                validate_max_executions(bad)
            with self.assertRaises(ValueError):
                validate_reservation_minutes(bad)

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            validate_max_executions(1.5)

    def test_ceiling_enforced(self):
        with self.assertRaises(ValueError):
            validate_max_executions(MAX_EXECUTIONS_CEILING + 1)
        with self.assertRaises(ValueError):
            validate_reservation_minutes(RESERVATION_MINUTES_CEILING + 1)


# ════════════════════════════════════════════════════════════════════
# 3–4. Capacity enforcement and concurrency
# ════════════════════════════════════════════════════════════════════


class TestCapacityEnforcement(CapacityTestBase):
    """The slot count is real, enforced atomically inside the gate."""

    def test_task_stores_both_settings(self):
        tid = _task(max_executions=100, reservation_minutes=30)
        task = db.get_task(tid)
        self.assertEqual(task["max_executions"], 100)
        self.assertEqual(task["reservation_minutes"], 30)

    def test_concurrent_starts_never_exceed_the_limit(self):
        tid = _task(max_executions=5)
        gate = TaskStartGate()
        for uid in range(1, 12):
            _user(uid)
        successes: list[int] = []
        lock = threading.Lock()

        def attempt(uid):
            try:
                gate.start(uid, tid)
                with lock:
                    successes.append(uid)
            except StartGateError:
                pass

        threads = [
            threading.Thread(target=attempt, args=(uid,))
            for uid in range(1, 12)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 5)
        self.assertEqual(claimed_capacity(tid), 5)

    def test_sequential_starts_beyond_limit_refused(self):
        tid = _task(max_executions=2)
        gate = TaskStartGate()
        _user(1); _user(2); _user(3)
        gate.start(1, tid)
        gate.start(2, tid)
        with self.assertRaises(StartGateError):
            gate.start(3, tid)

    def test_unlimited_task_has_no_limit(self):
        tid = _task()  # legacy: both NULL
        gate = TaskStartGate()
        for uid in range(1, 8):
            _user(uid)
            gate.start(uid, tid)
        self.assertEqual(claimed_capacity(tid), 7)

    def test_approved_counts_toward_limit(self):
        tid = _task(max_executions=2)
        gate = TaskStartGate()
        _user(1); _user(2); _user(3)
        gate.start(1, tid)
        gate.start(2, tid)
        rows = self._raw(
            "SELECT submission_id FROM task_submissions "
            "WHERE user_id=? AND task_id=?", 1, tid,
        )
        if rows:
            # a passed submission occupies its slot forever
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE task_submissions SET status='passed' "
                    "WHERE submission_id=?",
                    (rows[0][0],),
                )
        self.assertGreaterEqual(claimed_capacity(tid), 2)
        with self.assertRaises(StartGateError):
            gate.start(3, tid)


# ════════════════════════════════════════════════════════════════════
# 5–6. Reservation expiry releases capacity
# ════════════════════════════════════════════════════════════════════


class TestReservationExpiry(CapacityTestBase):
    """Expiry frees a slot; a submitted proof keeps it."""

    def test_expired_cycle_returns_to_available(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        self.assertEqual(
            db.get_user_task(1, tid)["status"], db.USER_TASK_STATUS_STARTED
        )
        self.assertEqual(claimed_capacity(tid), 1)

        self._force_expiry(tid, [1])
        self.assertTrue(release_expired_reservation(1, tid))
        row = db.get_user_task(1, tid)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_AVAILABLE)
        self.assertIsNone(row["reserved_until"])
        self.assertEqual(claimed_capacity(tid), 0)

    def test_released_slot_can_be_retaken(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1); _user(2)
        gate.start(1, tid)
        with self.assertRaises(StartGateError):
            gate.start(2, tid)  # full
        self._force_expiry(tid, [1])
        release_expired_reservation(1, tid)
        gate.start(2, tid)  # now there is room
        self.assertEqual(claimed_capacity(tid), 1)

    def test_reservation_within_window_is_not_released(self):
        tid = _task(reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        row = db.get_user_task(1, tid)
        self.assertFalse(is_reservation_expired(row, db.get_task(tid)))
        self.assertFalse(release_expired_reservation(1, tid))

    def test_legacy_task_reservation_never_expires(self):
        tid = _task()  # reservation_minutes NULL
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        row = db.get_user_task(1, tid)
        self.assertIsNone(row["reserved_until"])
        self.assertFalse(is_reservation_expired(row, db.get_task(tid)))
        self.assertEqual(db.task_claimed(tid) if hasattr(db, "task_claimed")
                         else claimed_capacity(tid), 1)

    def test_unreadable_deadline_is_never_expired(self):
        task = {"reservation_minutes": 30}
        for bad in (None, "", "not-a-date", "9999-99-99T00:00:00"):
            self.assertFalse(
                is_reservation_expired({"reserved_until": bad}, task)
            )

    def test_deadline_is_derived_from_the_task_policy(self):
        task = {"reservation_minutes": 60}
        self.assertEqual(
            reservation_deadline(task, "2026-01-01T00:00:00+00:00"),
            "2026-01-01T01:00:00+00:00",
        )
        self.assertIsNone(reservation_deadline(
            {"reservation_minutes": None}, "2026-01-01T00:00:00+00:00"
        ))

    def test_sweep_releases_every_lapsed_reservation(self):
        tid = _task(max_executions=10, reservation_minutes=30)
        gate = TaskStartGate()
        users = [1, 2, 3, 4]
        for u in users:
            _user(u)
        gate.start(1, tid); gate.start(2, tid)
        gate.start(3, tid); gate.start(4, tid)
        self._force_expiry(tid, [1, 2, 3])
        self.assertEqual(sweep_expired_reservations(), 3)
        self.assertEqual(claimed_capacity(tid), 1)


# ════════════════════════════════════════════════════════════════════
# 7–10. Repeat policy interplay
# ════════════════════════════════════════════════════════════════════


class TestRepeatAndReservation(CapacityTestBase):
    def test_repeatable_cycle_respects_total_limit(self):
        """A repeatable task's TOTAL cap counts every execution, not
        just one per user.  The second user is allowed; a third, beyond
        the declared total of 2, is not."""
        tid = _task(
            max_executions=2,
            reservation_minutes=60,
            repeat_policy="repeatable",
            repeat_hours=1,
        )
        gate = TaskStartGate()
        _user(1); _user(2); _user(3)
        gate.start(1, tid)
        gate.start(2, tid)
        self.assertEqual(claimed_capacity(tid), 2)
        with self.assertRaises(StartGateError):
            gate.start(3, tid)

    def test_one_time_cannot_repeat_after_approval(self):
        tid = _task(repeat_policy="one_time")
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        db.update_user_task_status(1, tid, "completed", _allow_completion=True)
        from task_attempt import TaskAttemptPolicy
        self.assertFalse(TaskAttemptPolicy.is_repeat_ready(
            1, tid, task=db.get_task(tid),
            user_task=db.get_user_task(1, tid),
        ))
        with self.assertRaises(StartGateError):
            gate.start(1, tid)

    def test_one_concurrent_cycle_per_user(self):
        """No two concurrent cycles for the same user/task."""
        tid = _task(repeat_policy="repeatable", repeat_hours=1,
                    reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        with self.assertRaises(StartGateError):
            gate.start(1, tid)  # already started

    def test_proof_awaiting_review_blocks_a_new_cycle(self):
        tid = _task(repeat_policy="repeatable", repeat_hours=1,
                    max_executions=5)
        gate = TaskStartGate()
        _user(1)   # the approver
        _user(2)   # the worker
        gate.start(2, tid)
        # a proof is now pending review
        ManualProofService.submit(2, tid, "https://example.com/proof")
        self.assertGreaterEqual(claimed_capacity(tid), 1)
        # the cycle is still STARTED: no second concurrent cycle exists
        self.assertEqual(
            db.get_user_task(2, tid)["status"], db.USER_TASK_STATUS_STARTED
        )
        with self.assertRaises(StartGateError):
            gate.start(2, tid)  # cannot run a concurrent cycle

    def test_sweep_leaves_a_submitted_proof_alone(self):
        """Expiry must never undo an awaiting-review submission."""
        tid = _task(reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)   # the approver
        _user(2)   # the worker
        gate.start(2, tid)
        ManualProofService.submit(2, tid, "https://example.com/proof")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE user_tasks SET reserved_until=? "
                "WHERE user_id=? AND task_id=?",
                ("2000-01-01T00:00:00+00:00", 2, tid),
            )
        self.assertEqual(sweep_expired_reservations(), 0)
        self.assertEqual(
            db.get_user_task(2, tid)["status"], db.USER_TASK_STATUS_STARTED
        )


# ════════════════════════════════════════════════════════════════════
# 12. Legacy tasks keep their exact previous behaviour
# ════════════════════════════════════════════════════════════════════


class TestLegacyCompatibility(CapacityTestBase):
    """Rows created before MT-TASK-28 must behave identically."""

    def test_legacy_row_has_nulls_and_no_enforcement(self):
        tid = _task()  # no limits supplied at all
        task = db.get_task(tid)
        self.assertIsNone(task["max_executions"])
        self.assertIsNone(task["reservation_minutes"])

        gate = TaskStartGate()
        for uid in range(1, 6):
            _user(uid)
            gate.start(uid, tid)
        self.assertEqual(claimed_capacity(tid), 5)  # unlimited

        # no reservation was ever recorded
        row = db.get_user_task(1, tid)
        self.assertIsNone(row["reserved_until"])
        # expiry cannot touch it
        self.assertFalse(release_expired_reservation(1, tid))
        self.assertEqual(
            db.get_user_task(1, tid)["status"], db.USER_TASK_STATUS_STARTED
        )

    def test_migration_is_additive_and_idempotent(self):
        """Re-running init_db must not disturb existing rows."""
        tid = _task(max_executions=3, reservation_minutes=15)
        before = db.get_task(tid)
        db.init_db(self.db_path)          # second run
        db.init_db(self.db_path)          # third run
        after = db.get_task(tid)
        self.assertEqual(before["max_executions"], after["max_executions"])
        self.assertEqual(
            before["reservation_minutes"], after["reservation_minutes"]
        )
        self.assertEqual(before["reward_units"], after["reward_units"])
        self.assertEqual(before["repeat_policy"], after["repeat_policy"])

    def test_migration_nullifies_garbage_values(self):
        tid = _task(max_executions=3)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE tasks SET max_executions=-5 WHERE id=?", (tid,)
            )
        db.init_db(self.db_path)
        self.assertIsNone(db.get_task(tid)["max_executions"])

    def test_migration_preserves_other_columns(self):
        tid = _task(max_executions=7, reservation_minutes=45)
        before = db.get_task(tid)
        db.init_db(self.db_path)
        after = db.get_task(tid)
        for key in ("title", "description", "type", "reward", "active",
                    "repeat_policy", "repeat_hours", "created_at"):
            self.assertEqual(before[key], after[key], key)


# ════════════════════════════════════════════════════════════════════
# 11. No financial side effect from any of this
# ════════════════════════════════════════════════════════════════════


class TestNoFinancialEffect(CapacityTestBase):
    def test_starting_never_moves_money(self):
        tid = _task(max_executions=2, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        wallets = self._raw("SELECT COUNT(*) FROM wallets")
        ledger = self._raw("SELECT COUNT(*) FROM ledger")
        self.assertEqual(wallets[0][0], 0)
        self.assertEqual(ledger[0][0], 0)

    def test_expiry_never_pays_or_completes(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        self._force_expiry(tid, [1])
        sweep_expired_reservations()
        wallets = self._raw("SELECT COUNT(*) FROM wallets")
        ledger = self._raw("SELECT COUNT(*) FROM ledger")
        submissions = self._raw("SELECT COUNT(*) FROM task_submissions")
        # the cycle is merely back to available — no money, no verdict
        self.assertEqual(wallets[0][0], 0)
        self.assertEqual(ledger[0][0], 0)
        self.assertEqual(submissions[0][0], 0)
        self.assertEqual(
            db.get_user_task(1, tid)["status"], db.USER_TASK_STATUS_AVAILABLE
        )

    def test_capacity_limit_is_not_a_financial_verdict(self):
        tid = _task(max_executions=1)
        gate = TaskStartGate()
        _user(1); _user(2)
        gate.start(1, tid)
        with self.assertRaises(StartGateError):
            gate.start(2, tid)
        # user 2 is untouched: no row, no money, no rejection record
        self.assertIsNone(db.get_user_task(2, tid))
        self.assertEqual(self._raw("SELECT COUNT(*) FROM wallets")[0][0], 0)


# ════════════════════════════════════════════════════════════════════
# 13. The limits reach the task row through EVERY creation path
# ════════════════════════════════════════════════════════════════════


class TestLimitsReachTheTaskRow(CapacityTestBase):
    """The advertiser's limits are not lost on the way to the row.

    The wizard is only ONE of the creation surfaces, so the limits must
    also survive the generic ``db.create_task`` and the canonical
    ``create_task_from_spec`` path — otherwise a task created outside
    the wizard would silently be unlimited.
    """

    def test_db_create_task_persists_both_limits(self):
        tid = _task(max_executions=12, reservation_minutes=45)
        task = db.get_task(tid)
        self.assertEqual(task["max_executions"], 12)
        self.assertEqual(task["reservation_minutes"], 45)

    def test_db_create_task_rejects_a_non_positive_limit(self):
        """The database layer is the last line of defence: a limit that
        is not a positive integer never reaches a row."""
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                _task(max_executions=bad)
            with self.assertRaises(ValueError):
                _task(reservation_minutes=bad)

    def test_task_spec_carries_the_limits_into_the_row(self):
        """``TaskSpec`` → ``create_task_from_spec`` → the task row."""
        from task_creation import TaskSpec, create_task_from_spec

        spec = TaskSpec(
            title="مهمة",
            description="وصف",
            provider="other",
            action="follow",
            target={"ref": "https://example.com/x"},
            verification="manual",
            reward=10,
            approver_id=1,
            max_executions=7,
            reservation_minutes=20,
        )
        tid = create_task_from_spec(spec)
        task = db.get_task(tid)
        self.assertEqual(task["max_executions"], 7)
        self.assertEqual(task["reservation_minutes"], 20)

    def test_task_spec_without_limits_stays_legacy(self):
        """A spec that carries no limits keeps the legacy behaviour."""
        from task_creation import TaskSpec, create_task_from_spec

        spec = TaskSpec(
            title="مهمة",
            description="وصف",
            provider="other",
            action="follow",
            target={"ref": "https://example.com/x"},
            verification="manual",
            reward=10,
            approver_id=1,
        )
        tid = create_task_from_spec(spec)
        task = db.get_task(tid)
        self.assertIsNone(task["max_executions"])
        self.assertIsNone(task["reservation_minutes"])


# ═════════════════════════════════════════════════════════════════════
# 14. Auto-release of expired reservation at start time
# ═════════════════════════════════════════════════════════════════════


class TestAutoReleaseExpiredAtStart(CapacityTestBase):
    """The start gate releases a user's own expired reservation so the
    cycle can be retried — without a background sweeper.
    """

    def test_start_releases_own_expired_reservation(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)
        self.assertEqual(
            db.get_user_task(1, tid)["status"], db.USER_TASK_STATUS_STARTED
        )

        self._force_expiry(tid, [1])

        result = gate.start(1, tid)
        self.assertTrue(result.success)
        row = db.get_user_task(1, tid)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)
        self.assertIsNotNone(row["reserved_until"])
        self.assertEqual(claimed_capacity(tid), 1)

    def test_start_with_unexpired_reservation_still_rejected(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1)
        gate.start(1, tid)

        with self.assertRaises(StartGateError):
            gate.start(1, tid)

    def test_expired_reservation_frees_capacity_for_another_user(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1); _user(2)
        gate.start(1, tid)
        self._force_expiry(tid, [1])

        gate.start(2, tid)
        self.assertEqual(claimed_capacity(tid), 1)

    def test_concurrent_expired_release_and_start(self):
        tid = _task(max_executions=1, reservation_minutes=30)
        gate = TaskStartGate()
        _user(1); _user(2)
        gate.start(1, tid)
        self._force_expiry(tid, [1])

        successes: list[int] = []
        lock = threading.Lock()

        def attempt(uid):
            try:
                gate.start(uid, tid)
                with lock:
                    successes.append(uid)
            except StartGateError:
                pass

        threads = [
            threading.Thread(target=attempt, args=(uid,))
            for uid in (1, 2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(claimed_capacity(tid), 1)


# ═════════════════════════════════════════════════════════════════════
# 15. Completion clears the reservation
# ═════════════════════════════════════════════════════════════════════


class TestCompletionClearsReservation(CapacityTestBase):
    """When a task completes, its reservation must be released so a
    repeatable cycle can begin again.
    """

    def test_completion_clears_reserved_until(self):
        tid = _task(max_executions=5, reservation_minutes=30)
        gate = TaskStartGate()
        completion = CompletionGate()
        _user(1)
        gate.start(1, tid)
        row_before = db.get_user_task(1, tid)
        self.assertIsNotNone(row_before["reserved_until"])

        result = completion.complete(
            1, tid,
            VerificationResult(VerificationStatus.PASSED),
        )
        self.assertTrue(result)

        row_after = db.get_user_task(1, tid)
        self.assertEqual(row_after["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNone(row_after["reserved_until"])
        self.assertIsNotNone(row_after["completed_at"])

    def test_completion_clears_reservation_on_legacy_task(self):
        tid = _task()
        gate = TaskStartGate()
        completion = CompletionGate()
        _user(1)
        gate.start(1, tid)
        self.assertIsNone(db.get_user_task(1, tid)["reserved_until"])

        completion.complete(
            1, tid,
            VerificationResult(VerificationStatus.PASSED),
        )
        row = db.get_user_task(1, tid)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_COMPLETED)
        self.assertIsNone(row["reserved_until"])

    def test_repeatable_cycle_after_completion_gets_fresh_reservation(self):
        tid = _task(
            max_executions=5,
            reservation_minutes=30,
            repeat_policy="repeatable",
            repeat_hours=1,
        )
        gate = TaskStartGate()
        completion = CompletionGate()
        _user(1)
        gate.start(1, tid)
        completion.complete(
            1, tid,
            VerificationResult(VerificationStatus.PASSED),
        )

        # Backdate completed_at so the cooldown is considered elapsed.
        past = "2000-01-01 00:00:00"
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE user_tasks SET completed_at = ? "
                "WHERE user_id = 1 AND task_id = ?",
                (past, tid),
            )

        result = gate.start(1, tid)
        self.assertTrue(result.success)
        row = db.get_user_task(1, tid)
        self.assertEqual(row["status"], db.USER_TASK_STATUS_STARTED)
        self.assertIsNotNone(row["reserved_until"])


if __name__ == "__main__":
    unittest.main()
