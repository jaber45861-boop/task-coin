"""
Secure Task Start Gate
======================

Internal service for safely starting a task for a user.

Flow:
    Task  →  Start Gate  →  user_tasks.status = started
                             ↓
                           Verification (task_verifier)
                             ↓
                           Completion Gate (task_completion)
                             ↓
                           user_tasks.status = completed

Responsibilities:
    - Validate user and task exist.
    - Validate task is active.
    - Validate user hasn't already started or completed this task.
    - Transition user_tasks from available → started.
    - MT-TASK-04: for a *repeatable* task whose cooldown has elapsed
      (read-only check via TaskAttemptPolicy.is_repeat_ready), start a
      new cycle: completed → started.  The state machine gains no new
      state; previous cycles remain historical in task_submissions.

NOT responsible for:
    - Verifying the task.
    - Completing the task.
    - Awarding rewards, modifying balances, referrals, or wallets.
    - Telegram API calls or Mini App calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import db
from task_attempt import TaskAttemptPolicy

logger = logging.getLogger(__name__)


# ── Start Gate Result ────────────────────────────────────────────


@dataclass(frozen=True)
class StartResult:
    """Immutable result from the TaskStartGate.

    Attributes:
        success:  Whether the task was started.
        message:  Human-readable result explanation.
        status:   The resulting user_task status (or current status on failure).
    """
    success: bool
    message: str
    status: str


# ── Start Gate Errors ────────────────────────────────────────────


class StartGateError(Exception):
    """Raised when the start gate rejects a request."""


# ── Task Start Gate ──────────────────────────────────────────────


class TaskStartGate:
    """The only acceptable path to start a task.

    Usage:
        gate = TaskStartGate()
        result = gate.start(user_id, task_id)

    The gate:
    - Validates user and task exist.
    - Validates task is active.
    - Validates user_task is in 'available' status.
    - Transitions to 'started' inside db.transaction() (BEGIN IMMEDIATE)
      so the validate-then-transition sequence is atomic and concurrent
      starts of the same user/task cannot race (TOCTOU-safe).
    - Does NOT verify, complete, award rewards, or modify referrals.
    """

    def start(self, user_id: int, task_id: int) -> StartResult:
        """Start a task for a user.

        Args:
            user_id: Telegram user ID.
            task_id: Task definition ID.

        Returns:
            StartResult with success=True on successful start.

        Raises:
            StartGateError: If any pre-condition fails (user/task not found,
                task inactive, invalid state).
        """
        # The whole read/modify/write sequence runs inside one existing
        # db.transaction() — BEGIN IMMEDIATE takes the write lock before
        # any validation read, so concurrent starts for the same
        # user/task serialize: exactly one observes 'available' and the
        # rest are rejected with the usual errors.
        with db.transaction() as conn:
            # 1. User must exist
            user = db.get_user(user_id)
            if user is None:
                raise StartGateError(f"User {user_id} not found")

            # 2. Task must exist
            task = db.get_task(task_id)
            if task is None:
                raise StartGateError(f"Task {task_id} not found")

            # 3. Task must be active
            if not task["active"]:
                raise StartGateError(f"Task {task_id} is not active")

            # 4. Check current user_task state (inside the transaction,
            #    so no other writer can change it before the transition)
            user_task = db.get_user_task(user_id, task_id)

            if user_task is not None:
                current_status = user_task["status"]

                # Completed → terminal for one_time tasks; for a
                # repeatable task, a new cycle may start once the
                # cooldown has elapsed (eligibility is computed from
                # the persisted completed_at timestamp — server time,
                # never client input; MT-TASK-04).
                if current_status == db.USER_TASK_STATUS_COMPLETED:
                    if TaskAttemptPolicy.is_repeat_ready(
                        user_id, task_id,
                        task=task, user_task=user_task,
                    ):
                        # completed → started (guarded compare-and-set).
                        # completed_at is cleared for the NEW cycle;
                        # the finished cycle's history stays in
                        # task_submissions (never erased here).
                        cursor = conn.execute(
                            "UPDATE user_tasks "
                            "SET status = ?, "
                            "    started_at = CURRENT_TIMESTAMP, "
                            "    completed_at = NULL "
                            "WHERE user_id = ? AND task_id = ? "
                            "AND status = ?",
                            (
                                db.USER_TASK_STATUS_STARTED,
                                user_id,
                                task_id,
                                db.USER_TASK_STATUS_COMPLETED,
                            ),
                        )
                        if cursor.rowcount != 1:
                            # Lost a race — report with the existing
                            # error vocabulary.
                            latest = db.get_user_task(user_id, task_id)
                            latest_status = latest["status"] if latest else None
                            if latest_status == db.USER_TASK_STATUS_STARTED:
                                raise StartGateError(
                                    f"Task {task_id} already started for user {user_id}"
                                )
                            raise StartGateError(
                                f"Task {task_id} already completed for user {user_id}"
                            )
                        logger.info(
                            "Repeatable task cycle started via gate: "
                            "user=%d task=%d",
                            user_id, task_id,
                        )
                        return StartResult(
                            success=True,
                            message=f"Task {task_id} started for user {user_id}",
                            status=db.USER_TASK_STATUS_STARTED,
                        )

                    # one_time (or cooldown not elapsed) → terminal
                    raise StartGateError(
                        f"Task {task_id} already completed for user {user_id}"
                    )

                # Already started → reject (idempotent, no mutation)
                if current_status == db.USER_TASK_STATUS_STARTED:
                    raise StartGateError(
                        f"Task {task_id} already started for user {user_id}"
                    )

                # available → started (guarded compare-and-set below)
                cursor = conn.execute(
                    "UPDATE user_tasks "
                    "SET status = ?, started_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ? AND task_id = ? AND status = ?",
                    (
                        db.USER_TASK_STATUS_STARTED,
                        user_id,
                        task_id,
                        db.USER_TASK_STATUS_AVAILABLE,
                    ),
                )
                if cursor.rowcount != 1:
                    # Unreachable while BEGIN IMMEDIATE serializes
                    # writers — re-read so the caller still gets the
                    # exact existing error vocabulary.
                    latest = db.get_user_task(user_id, task_id)
                    latest_status = latest["status"] if latest else None
                    if latest_status == db.USER_TASK_STATUS_COMPLETED:
                        raise StartGateError(
                            f"Task {task_id} already completed for user {user_id}"
                        )
                    raise StartGateError(
                        f"Task {task_id} already started for user {user_id}"
                    )

            else:
                # No user_task row exists → create it directly in the
                # 'started' state (user/task existence was validated
                # above inside this same transaction).
                conn.execute(
                    "INSERT INTO user_tasks "
                    "(user_id, task_id, status, started_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                    (user_id, task_id, db.USER_TASK_STATUS_STARTED),
                )

        logger.info("Task started via gate: user=%d task=%d", user_id, task_id)
        return StartResult(
            success=True,
            message=f"Task {task_id} started for user {user_id}",
            status=db.USER_TASK_STATUS_STARTED,
        )
