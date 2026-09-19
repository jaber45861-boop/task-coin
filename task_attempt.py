"""
Task Attempt Policy Boundary
=============================

Controls whether a user is currently allowed to submit another
verification attempt for a STARTED task.

Flow:
    TaskStartGate.start()
        ↓
    TaskAttemptPolicy.can_submit(user_id, task_id)
        ↓
    TaskSubmissionService.submit(...)
        ↓
    TaskVerifier.verify(context)
        ↓
    VerificationResult(PASSED | FAILED | ERROR)
        ↓
    The caller (separate, external)

Responsibilities:
    - Validate user, task, and user_task state.
    - Answer only: "Is this user currently allowed to submit?"
    - Return AttemptResult(allowed / rejected / reason).

NOT responsible for:
    - Performing verification.
    - Completing a task.
    - Starting a task.
    - Modifying user_tasks, users, tasks, balances, referrals, or wallets.
    - Telegram API calls or Mini App calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import db

logger = logging.getLogger(__name__)


# ── Attempt Result ────────────────────────────────────────────────


@dataclass(frozen=True)
class AttemptResult:
    """Result of an attempt-policy check.

    Attributes:
        allowed:  True if submission is permitted.
        reason:   Human-readable explanation (empty when allowed).
    """

    allowed: bool
    reason: str = ""


# ── Attempt Policy ────────────────────────────────────────────────


class TaskAttemptPolicy:
    """Read-only policy layer that decides whether a user may submit
    a verification attempt for a given task.

    This policy is purely a gate — it inspects the database but
    never writes to it.
    """

    @staticmethod
    def can_submit(user_id: int, task_id: int) -> AttemptResult:
        """Check whether the user is currently allowed to submit.

        Args:
            user_id: Telegram user ID.
            task_id: Task definition ID.

        Returns:
            AttemptResult with allowed=True if submission is permitted,
            or allowed=False with a reason string otherwise.
        """
        # ── 1. Validate user exists ───────────────────────────
        user = db.get_user(user_id)
        if user is None:
            return AttemptResult(
                allowed=False,
                reason=f"User {user_id} not found",
            )

        # ── 2. Validate task exists ───────────────────────────
        task = db.get_task(task_id)
        if task is None:
            return AttemptResult(
                allowed=False,
                reason=f"Task {task_id} not found",
            )

        # ── 3. Validate task is active ────────────────────────
        if not task["active"]:
            return AttemptResult(
                allowed=False,
                reason=f"Task {task_id} is not active",
            )

        # ── 4. Validate user_task exists ──────────────────────
        user_task = db.get_user_task(user_id, task_id)
        if user_task is None:
            return AttemptResult(
                allowed=False,
                reason=(
                    f"No user_task record for user={user_id}, task={task_id}"
                ),
            )

        # ── 5. Validate user_task status is STARTED ───────────
        current_status = user_task["status"]

        if current_status == db.USER_TASK_STATUS_STARTED:
            return AttemptResult(allowed=True)

        # All other states are rejected
        return AttemptResult(
            allowed=False,
            reason=(
                f"Task {task_id} is not in started state "
                f"(current: {current_status})"
            ),
        )
