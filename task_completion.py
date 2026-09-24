"""
Secure Task Completion Gate
===========================
Single entry point for completing tasks.  The gate enforces:

1. A VerificationResult must be provided (verification contract).
2. Only the gate can transition user_tasks from started → completed.
3. MT-REWARD-01: the task reward is credited atomically WITH the
   transition — wallet credit + ledger entry run inside the same
   db.transaction() via TaskRewardService, so completion and money
   commit or roll back together.  No referral changes occur here.

Flow:
    Task  →  Verification  →  Verified result  →  Completion gate
        →  user_tasks.status = completed  +  wallet credit  +  ledger entry
           (one BEGIN IMMEDIATE transaction)

No user-facing code should call database completion directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import db
from task_reward import TaskRewardService

logger = logging.getLogger(__name__)


# ── Verification Contract ──────────────────────────────────────────

class VerificationStatus(Enum):
    """Possible outcomes of a task verification attempt."""
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True)
class VerificationResult:
    """Immutable contract for task verification outcomes.

    Attributes:
        status: Whether verification passed, failed, or hit an error.
        reason: Human-readable explanation (empty string on success).
    """
    status: VerificationStatus
    reason: str = ""

    @property
    def passed(self) -> bool:
        """Return True only when status is PASSED."""
        return self.status == VerificationStatus.PASSED


# ── Completion Gate Errors ─────────────────────────────────────────

class CompletionGateError(Exception):
    """Raised when the completion gate rejects a request."""


# ── Completion Gate ────────────────────────────────────────────────

class CompletionGate:
    """The only acceptable path from started → completed.

    Usage:
        gate = CompletionGate()
        result = gate.complete(user_id, task_id, verification_result)

    The gate:
    - Validates user and task exist.
    - Validates user_task exists and is in 'started' status.
    - Requires a passed VerificationResult.
    - Transitions to 'completed' inside db.transaction() (BEGIN IMMEDIATE)
      so the validate-then-transition sequence is atomic: exactly one
      concurrent attempt can move started → completed.
    - MT-REWARD-01: settles the task reward on that SAME transaction
      (wallet credit + ledger entry through TaskRewardService) — any
      financial failure rolls the transition back, so there is never a
      completed task without its reward nor a reward without its
      completion.  No referral or other wallet state is touched.
    """

    def complete(
        self,
        user_id: int,
        task_id: int,
        verification: VerificationResult,
    ) -> bool:
        """Complete a task after verification passes.

        Args:
            user_id: Telegram user ID.
            task_id: Task definition ID.
            verification: The VerificationResult from a verifier.

        Returns:
            True on successful completion.

        Raises:
            CompletionGateError: If any pre-condition fails.
        """
        # 1. Verification must have passed
        if not verification.passed:
            raise CompletionGateError(
                f"Verification failed ({verification.status.value}): "
                f"{verification.reason or 'no reason provided'}"
            )

        # Validation and the transition run inside one existing
        # db.transaction() — BEGIN IMMEDIATE takes the write lock first,
        # so concurrent successful submissions for the same user/task
        # serialize: exactly one observes 'started' and completes; every
        # other attempt is rejected with the existing error vocabulary
        # instead of performing a second completion.
        with db.transaction() as conn:
            # 2. User must exist
            user = db.get_user(user_id)
            if user is None:
                raise CompletionGateError(f"User {user_id} not found")

            # 3. Task must exist
            task = db.get_task(task_id)
            if task is None:
                raise CompletionGateError(f"Task {task_id} not found")

            # 4. user_task must exist
            user_task = db.get_user_task(user_id, task_id)
            if user_task is None:
                raise CompletionGateError(
                    f"No user_task record for user={user_id}, task={task_id}"
                )

            # 5. Must be in 'started' status
            current_status = user_task["status"]
            if current_status != db.USER_TASK_STATUS_STARTED:
                raise CompletionGateError(
                    f"Cannot complete: current status is '{current_status}', "
                    f"expected '{db.USER_TASK_STATUS_STARTED}'"
                )

            # 6. Atomic started → completed (guarded compare-and-set on
            #    the transaction's own connection; 'completed' stays
            #    terminal and no reward/wallet/ledger state is touched)
            cursor = conn.execute(
                "UPDATE user_tasks "
                "SET status = ?, completed_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND task_id = ? AND status = ?",
                (
                    db.USER_TASK_STATUS_COMPLETED,
                    user_id,
                    task_id,
                    db.USER_TASK_STATUS_STARTED,
                ),
            )
            if cursor.rowcount != 1:
                # Unreachable while BEGIN IMMEDIATE serializes writers —
                # re-read so the caller still gets the exact existing
                # error vocabulary.
                latest = db.get_user_task(user_id, task_id)
                latest_status = latest["status"] if latest else None
                raise CompletionGateError(
                    f"Cannot complete: current status is '{latest_status}', "
                    f"expected '{db.USER_TASK_STATUS_STARTED}'"
                )

            # 7. Atomic reward settlement (MT-REWARD-01): credit the
            #    user's USDT wallet and record the ledger entry on THIS
            #    same transaction, after the transition succeeded —
            #    completion + wallet + ledger commit together.  Any
            #    failure (invalid reward, wallet write, ledger insert)
            #    propagates out of db.transaction(), which rolls back
            #    the compare-and-set above along with every financial
            #    write: no partial state is left behind.
            credited_units = TaskRewardService.settle_completion(
                conn, user_id=user_id, task_id=task_id, task=task
            )

        logger.info(
            "Task completed via gate: user=%d task=%d credited_units=%s",
            user_id,
            task_id,
            credited_units,
        )
        return True
