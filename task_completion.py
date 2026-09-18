"""
Secure Task Completion Gate
===========================
Single entry point for completing tasks.  The gate enforces:

1. A VerificationResult must be provided (verification contract).
2. Only the gate can transition user_tasks from started → completed.
3. No rewards, referral changes, or wallet modifications occur here.

Flow:
    Task  →  Verification  →  Verified result  →  Completion gate  →  user_tasks.status = completed

No user-facing code should call database completion directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import db

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
    - Transitions to 'completed' via db.update_user_task_status.
    - Does NOT grant rewards or modify referral/wallet state.
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

        # 6. Transition to completed (via internal guard)
        db.update_user_task_status(
            user_id,
            task_id,
            db.USER_TASK_STATUS_COMPLETED,
            _allow_completion=True,
        )

        logger.info(
            "Task completed via gate: user=%d task=%d",
            user_id,
            task_id,
        )
        return True
