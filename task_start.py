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
    - Transitions to 'started' via db.update_user_task_status.
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

        # 4. Check current user_task state
        user_task = db.get_user_task(user_id, task_id)

        if user_task is not None:
            current_status = user_task["status"]

            # Already completed → reject
            if current_status == db.USER_TASK_STATUS_COMPLETED:
                raise StartGateError(
                    f"Task {task_id} already completed for user {user_id}"
                )

            # Already started → reject (idempotent, no mutation)
            if current_status == db.USER_TASK_STATUS_STARTED:
                raise StartGateError(
                    f"Task {task_id} already started for user {user_id}"
                )

            # available → started (falls through to transition below)

        else:
            # No user_task row exists → create one first
            db.create_user_task(user_id, task_id)

        # 5. Transition available → started
        db.update_user_task_status(
            user_id,
            task_id,
            db.USER_TASK_STATUS_STARTED,
        )

        logger.info("Task started via gate: user=%d task=%d", user_id, task_id)
        return StartResult(
            success=True,
            message=f"Task {task_id} started for user {user_id}",
            status=db.USER_TASK_STATUS_STARTED,
        )
