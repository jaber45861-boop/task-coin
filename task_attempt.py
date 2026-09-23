"""
Task Attempt Policy Boundary
=============================

Controls whether a user is currently allowed to submit another
verification attempt for a STARTED task, and (MT-TASK-04) whether a
completed repeatable task may start a new cycle.

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
    - Return AttemptResult(allowed / rejected / reason / attempt_count).
    - Read persistent submission history (task_submissions) so future
      max-attempt rules have data without adding a rule now.
    - Compute repeat-cooldown eligibility from persisted timestamps and
      server time (is_repeat_ready).

NOT responsible for:
    - Performing verification.
    - Completing a task.
    - Starting a task.
    - Modifying user_tasks, tasks, task_submissions, users, balances,
      referrals, or wallets — this policy is strictly read-only.
    - Telegram API calls or Mini App calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import db
from task_submission_store import SubmissionRecord, TaskSubmissionStore

logger = logging.getLogger(__name__)


# ── Attempt Result ────────────────────────────────────────────────


@dataclass(frozen=True)
class AttemptResult:
    """Result of an attempt-policy check.

    Attributes:
        allowed:  True if submission is permitted.
        reason:   Human-readable explanation (empty when allowed).
        attempt_count: Number of persisted submission attempts for
            this user/task so far (0 when none).  Exposed for future
            max-attempt rules — no limit is enforced today.
    """

    allowed: bool
    reason: str = ""
    attempt_count: int = 0


# ── Attempt Policy ────────────────────────────────────────────────


class TaskAttemptPolicy:
    """Read-only policy layer that decides whether a user may submit
    a verification attempt for a given task, and whether a completed
    repeatable task is eligible for a new cycle.

    This policy is purely a gate — it inspects the database (including
    the persistent task_submissions history) but never writes to it.
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

        # Persistent attempt history (MT-TASK-04): read-only count,
        # exposed for future max-attempt rules — no limit is applied
        # here because the task model defines none today.
        attempt_count = TaskSubmissionStore.count_attempts(
            user_id, task_id
        )

        if current_status == db.USER_TASK_STATUS_STARTED:
            return AttemptResult(allowed=True, attempt_count=attempt_count)

        # All other states are rejected
        return AttemptResult(
            allowed=False,
            reason=(
                f"Task {task_id} is not in started state "
                f"(current: {current_status})"
            ),
            attempt_count=attempt_count,
        )

    # ── Persistent attempt history (MT-TASK-04) ─────────────────

    @staticmethod
    def attempt_count(user_id: int, task_id: int) -> int:
        """Number of persisted submission attempts for this user/task."""
        return TaskSubmissionStore.count_attempts(user_id, task_id)

    @staticmethod
    def attempt_history(
        user_id: int, task_id: int,
    ) -> list[SubmissionRecord]:
        """Full chronological attempt history (oldest first).

        Failed and error attempts remain listed forever — history is
        never erased by later attempts or completion.
        """
        return TaskSubmissionStore.list_user_task_submissions(
            user_id, task_id
        )

    # ── Repeat-cooldown eligibility (MT-TASK-04, read-only) ──────

    @staticmethod
    def is_repeat_ready(
        user_id: int,
        task_id: int,
        task: dict | None = None,
        user_task: dict | None = None,
    ) -> bool:
        """True when a completed repeatable task's cooldown has elapsed.

        Eligibility is calculated from persisted timestamps
        (user_tasks.completed_at, written by the completion gate using
        database time) against the current server time — never client
        input, never a background scheduler.

        Always False when:
        - the task is one_time (terminal for the user),
        - the repeat configuration is missing/invalid (defensive: a
          migrated row can only become repeatable through validated
          application code),
        - the user_task is not in completed state, or its persisted
          completed_at timestamp is unreadable.

        ``task`` / ``user_task`` may be passed by a caller that already
        read them (e.g. inside StartGate's transaction) to avoid a
        second read; they are never modified.
        """
        if task is None:
            task = db.get_task(task_id)
        if user_task is None:
            user_task = db.get_user_task(user_id, task_id)
        if task is None or user_task is None:
            return False
        if task.get("repeat_policy") != db.REPEAT_POLICY_REPEATABLE:
            return False
        repeat_hours = task.get("repeat_hours")
        if isinstance(repeat_hours, bool) or not isinstance(repeat_hours, int):
            return False
        if repeat_hours < 1:
            return False
        if user_task.get("status") != db.USER_TASK_STATUS_COMPLETED:
            return False
        completed_at = user_task.get("completed_at")
        if not completed_at:
            return False
        elapsed = _elapsed_seconds_since(completed_at)
        if elapsed is None:
            return False
        return elapsed >= repeat_hours * 3600


def _elapsed_seconds_since(timestamp: str) -> int | None:
    """Whole seconds between a persisted UTC timestamp and server now.

    Integer arithmetic only — no floats.  Returns None when the stored
    value does not match the repository's CURRENT_TIMESTAMP format.
    """
    try:
        then = datetime.strptime(
            timestamp, "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc)
    return int((now - then).total_seconds())
