"""
Focused tests — repeat policy, cooldown & attempt counting (MT-TASK-04)
=======================================================================

Covers the behavior of the domain layer added by MT-TASK-04:

TaskAttemptPolicy (STRICTLY READ-ONLY):
- is_repeat_ready(): cooldown eligibility computed from the persisted
  user_tasks.completed_at timestamp against server time.
  * one_time task            → never ready (terminal for the user)
  * cooldown not elapsed     → not ready
  * cooldown elapsed         → ready
  * not completed / missing completed_at / invalid hours → not ready
- attempt_count() / attempt_history(): persistent attempt history from
  task_submissions, oldest first, never erased by later attempts.
- None of these calls mutate any table.

TaskStartGate repeat cycle:
- one_time completed task stays terminal (existing behavior preserved).
- repeatable task before the cooldown → rejected, no mutation.
- repeatable task after the cooldown  → completed → started, with
  completed_at cleared for the NEW cycle.
- the new cycle is submittable again (can_submit allowed).
- task_submissions history is preserved across the new cycle.
- a second completion restarts the cooldown.

No network, no Telegram, no sleeps — timestamps are backdated directly,
so every test is deterministic.

Run:
    python3 -m pytest test_task_repeat_policy.py -v
"""

import pytest

import db
from task_attempt import TaskAttemptPolicy
from task_completion import CompletionGate, VerificationResult, VerificationStatus
from task_start import StartGateError, TaskStartGate
from task_submission_store import TaskSubmissionStore

USER = 21
HOURS = 24


@pytest.fixture
def path(monkeypatch, tmp_path):
    db_path = str(tmp_path / "repeat_policy_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "repeater", "Repeater")
    return db_path


def _one_time_task() -> int:
    return db.create_task(
        "One Time", "Do it once", "deterministic", 10
    )


def _repeatable_task() -> int:
    return db.create_task(
        "Repeatable", "Do it again", "deterministic", 10,
        repeat_policy=db.REPEAT_POLICY_REPEATABLE,
        repeat_hours=HOURS,
    )


def _complete(gate: TaskStartGate, task_id: int) -> None:
    """Start (if needed) and complete a task via the real gates."""
    user_task = db.get_user_task(USER, task_id)
    if user_task is None or user_task["status"] != db.USER_TASK_STATUS_STARTED:
        gate.start(USER, task_id)
    CompletionGate().complete(
        USER, task_id,
        VerificationResult(status=VerificationStatus.PASSED),
    )


def _backdate_completed(task_id: int, hours: int) -> None:
    """Move the persisted completed_at into the past (server-side data)."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE user_tasks SET completed_at = "
            "datetime('now', ?) "
            "WHERE user_id = ? AND task_id = ?",
            (f"-{hours} hours", USER, task_id),
        )


def _user_task_row(task_id: int) -> dict:
    row = db.get_user_task(USER, task_id)
    assert row is not None
    return row


def _submission_rows(task_id: int) -> list[dict]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT submission_id, user_id, task_id, attempt_number, "
            "       status, idempotency_key, verification_reason, "
            "       submitted_at, completed_at, created_at "
            "FROM task_submissions "
            "WHERE user_id = ? AND task_id = ? "
            "ORDER BY submission_id ASC",
            (USER, task_id),
        ).fetchall()
    return [dict(r) for r in rows]


def _seed_attempts(task_id: int, outcomes: list[str]) -> None:
    """Create persisted attempt records exactly as the pipeline does."""
    for i, status in enumerate(outcomes, start=1):
        record, created = TaskSubmissionStore.create_submission(
            USER, task_id, f"key-{i}"
        )
        assert created
        TaskSubmissionStore.record_verification_result(
            record.submission_id, status,
            None if status == db.SUBMISSION_STATUS_PASSED else "nope",
        )


# ════════════════════════════════════════════════════════════════════
# is_repeat_ready — cooldown eligibility (read-only)
# ════════════════════════════════════════════════════════════════════


class TestRepeatReady:
    def test_one_time_completed_is_never_ready(self, path):
        tid = _one_time_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False

    def test_repeatable_before_cooldown_not_ready(self, path):
        tid = _repeatable_task()
        _complete(TaskStartGate(), tid)
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False

    def test_repeatable_after_cooldown_is_ready(self, path):
        tid = _repeatable_task()
        _complete(TaskStartGate(), tid)
        _backdate_completed(tid, HOURS + 1)
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is True

    def test_repeatable_started_is_not_ready(self, path):
        tid = _repeatable_task()
        TaskStartGate().start(USER, tid)
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False

    def test_missing_completed_at_is_not_ready(self, path):
        tid = _repeatable_task()
        _complete(TaskStartGate(), tid)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE user_tasks SET completed_at = NULL "
                "WHERE user_id = ? AND task_id = ?",
                (USER, tid),
            )
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False

    def test_database_rejects_invalid_repeat_hours(self, path):
        """The schema itself forbids an invalid repeat_hours value."""
        import sqlite3
        tid = _repeatable_task()
        with db.get_connection() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tasks SET repeat_hours = 0 WHERE id = ?", (tid,)
                )

    @pytest.mark.parametrize("hours", [0, -1, None, True, 1.5])
    def test_defensive_invalid_hours_never_ready(self, path, hours):
        """A hand-built invalid task dict never opens a cooldown, even
        if it somehow bypassed the schema (migrated/legacy row)."""
        completed_at = "2020-01-01 00:00:00"
        ready = TaskAttemptPolicy.is_repeat_ready(
            USER, 1,
            task={
                "repeat_policy": db.REPEAT_POLICY_REPEATABLE,
                "repeat_hours": hours,
            },
            user_task={
                "status": db.USER_TASK_STATUS_COMPLETED,
                "completed_at": completed_at,
            },
        )
        assert ready is False

    def test_unknown_task_or_user_is_not_ready(self, path):
        tid = _repeatable_task()
        _complete(TaskStartGate(), tid)
        _backdate_completed(tid, HOURS + 1)
        assert TaskAttemptPolicy.is_repeat_ready(999999, tid) is False
        assert TaskAttemptPolicy.is_repeat_ready(USER, 999999) is False

    def test_is_repeat_ready_is_read_only(self, path):
        tid = _repeatable_task()
        _complete(TaskStartGate(), tid)
        _backdate_completed(tid, HOURS + 1)
        before_task = dict(_user_task_row(tid))
        before_subs = _submission_rows(tid)
        task_before = dict(db.get_task(tid))

        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is True

        assert dict(_user_task_row(tid)) == before_task
        assert _submission_rows(tid) == before_subs
        assert dict(db.get_task(tid)) == task_before


# ════════════════════════════════════════════════════════════════════
# Attempt counting / history (read-only policy)
# ════════════════════════════════════════════════════════════════════


class TestAttemptCounting:
    def test_count_matches_persisted_attempts(self, path):
        tid = _repeatable_task()
        TaskStartGate().start(USER, tid)
        assert TaskAttemptPolicy.attempt_count(USER, tid) == 0

        _seed_attempts(tid, [
            db.SUBMISSION_STATUS_FAILED,
            db.SUBMISSION_STATUS_ERROR,
        ])
        assert TaskAttemptPolicy.attempt_count(USER, tid) == 2

    def test_history_is_chronological_and_survives_completion(self, path):
        tid = _repeatable_task()
        TaskStartGate().start(USER, tid)
        _seed_attempts(tid, [
            db.SUBMISSION_STATUS_FAILED,
            db.SUBMISSION_STATUS_PASSED,
        ])
        CompletionGate().complete(
            USER, tid,
            VerificationResult(status=VerificationStatus.PASSED),
        )

        history = TaskAttemptPolicy.attempt_history(USER, tid)
        assert [r.attempt_number for r in history] == [1, 2]
        assert [r.status for r in history] == ["failed", "passed"]

    def test_attempt_count_is_read_only(self, path):
        tid = _repeatable_task()
        TaskStartGate().start(USER, tid)
        _seed_attempts(tid, [db.SUBMISSION_STATUS_FAILED])
        before_subs = _submission_rows(tid)
        before_ut = dict(_user_task_row(tid))

        TaskAttemptPolicy.attempt_count(USER, tid)
        TaskAttemptPolicy.attempt_history(USER, tid)
        TaskAttemptPolicy.can_submit(USER, tid)

        assert _submission_rows(tid) == before_subs
        assert dict(_user_task_row(tid)) == before_ut

    def test_can_submit_exposes_attempt_count(self, path):
        tid = _repeatable_task()
        TaskStartGate().start(USER, tid)
        _seed_attempts(tid, [db.SUBMISSION_STATUS_FAILED])
        result = TaskAttemptPolicy.can_submit(USER, tid)
        assert result.allowed is True
        assert result.attempt_count == 1


# ════════════════════════════════════════════════════════════════════
# StartGate repeat cycle
# ════════════════════════════════════════════════════════════════════


class TestStartGateRepeatCycle:
    def test_one_time_completed_stays_terminal(self, path):
        tid = _one_time_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        with pytest.raises(StartGateError) as exc:
            gate.start(USER, tid)
        assert "already completed" in str(exc.value)
        assert _user_task_row(tid)["status"] == db.USER_TASK_STATUS_COMPLETED

    def test_repeatable_before_cooldown_rejected_no_mutation(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        before = dict(_user_task_row(tid))

        with pytest.raises(StartGateError) as exc:
            gate.start(USER, tid)
        assert "already completed" in str(exc.value)
        assert dict(_user_task_row(tid)) == before

    def test_repeatable_after_cooldown_starts_new_cycle(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _backdate_completed(tid, HOURS + 1)

        result = gate.start(USER, tid)
        assert result.success is True
        assert result.status == db.USER_TASK_STATUS_STARTED

        row = _user_task_row(tid)
        assert row["status"] == db.USER_TASK_STATUS_STARTED
        assert row["completed_at"] is None
        assert row["started_at"] is not None

    def test_new_cycle_is_submittable_again(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _backdate_completed(tid, HOURS + 1)
        gate.start(USER, tid)

        allowed = TaskAttemptPolicy.can_submit(USER, tid)
        assert allowed.allowed is True

    def test_double_start_of_new_cycle_rejected(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _backdate_completed(tid, HOURS + 1)
        gate.start(USER, tid)

        with pytest.raises(StartGateError) as exc:
            gate.start(USER, tid)
        assert "already started" in str(exc.value)

    def test_submission_history_preserved_across_new_cycle(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _seed_attempts(tid, [
            db.SUBMISSION_STATUS_FAILED,
            db.SUBMISSION_STATUS_PASSED,
        ])
        before = _submission_rows(tid)
        _backdate_completed(tid, HOURS + 1)

        gate.start(USER, tid)

        after = _submission_rows(tid)
        assert after == before
        assert TaskAttemptPolicy.attempt_count(USER, tid) == 2

    def test_second_completion_restarts_cooldown(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _backdate_completed(tid, HOURS + 1)
        gate.start(USER, tid)
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False

        CompletionGate().complete(
            USER, tid,
            VerificationResult(status=VerificationStatus.PASSED),
        )
        # Fresh completed_at → cooldown counts from the SECOND cycle.
        assert TaskAttemptPolicy.is_repeat_ready(USER, tid) is False
        with pytest.raises(StartGateError):
            gate.start(USER, tid)

    def test_repeat_cycle_creates_no_new_user_task_row(self, path):
        tid = _repeatable_task()
        gate = TaskStartGate()
        _complete(gate, tid)
        _backdate_completed(tid, HOURS + 1)
        gate.start(USER, tid)

        with db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM user_tasks "
                "WHERE user_id = ? AND task_id = ?",
                (USER, tid),
            ).fetchone()["c"]
        assert count == 1
