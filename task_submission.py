"""
Secure Task Submission Boundary
================================

Accepts user-provided actual verification data for a STARTED task,
builds the VerificationContext, invokes the existing verifier, and
returns VerificationResult.

Flow:
    TaskStartGate.start()
        ↓
    TaskSubmissionService.submit(user_id, task_id, actual_data)
        ↓
    TaskVerifier.verify(context)
        ↓
    VerificationResult(PASSED | FAILED | ERROR)
        ↓
    The caller (separate, external)

Responsibilities:
    - Validate user, task, and user_task state.
    - Reject submissions for non-STARTED tasks.
    - Read expected_data from the task definition (not from user).
    - Accept actual_data from the user (submission data only).
    - Build an immutable VerificationContext.
    - Invoke the registered verifier for the task type.
    - Return VerificationResult without completing the task.

NOT responsible for:
    - Starting a task (TaskStartGate).
    - Completing a task (done by external caller).
    - Awarding rewards, modifying balances, referrals, or wallets.
    - Telegram API calls or Mini App calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import db
from task_attempt import TaskAttemptPolicy
from task_verifier import (
    FrozenDict,
    VerificationContext,
    freeze_value,
    get_verifier,
)
from task_completion import VerificationResult, VerificationStatus

logger = logging.getLogger(__name__)


# ── Forbidden Fields ──────────────────────────────────────────────

# Fields that must NEVER be accepted from user submission data.
# These are task-configuration or system-controlled fields.
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "completed",
    "status",
    "reward",
    "active",
    "expected",
    "task_type",
    "task_id",
    "user_id",
    "created_at",
    "started_at",
    "completed_at",
})


# ── Submission Errors ─────────────────────────────────────────────

class SubmissionError(Exception):
    """Raised when submission validation fails."""


# ── Submission Service ────────────────────────────────────────────

class TaskSubmissionService:
    """Secure internal submission layer for task verification data.

    Usage:
        result = TaskSubmissionService.submit(
            user_id=123,
            task_id=1,
            actual_data={"answer": "correct"},
        )

        # result is VerificationResult(PASSED | FAILED | ERROR)
        # Task status remains STARTED — caller must use the completion gate.
    """

    @staticmethod
    def submit(
        user_id: int,
        task_id: int,
        actual_data: dict,
    ) -> VerificationResult:
        """Submit actual verification data for a STARTED task.

        Args:
            user_id:    Telegram user ID.
            task_id:    Task definition ID.
            actual_data: User-provided verification data (dict).

        Returns:
            VerificationResult with status PASSED, FAILED, or ERROR.

        Raises:
            SubmissionError: If pre-validation fails (user/task
                not found, task inactive, task not started, etc.).
        """
        # ── 1. Attempt policy gate ────────────────────────────
        #    Delegates user/task/state validation to the policy.
        #    The policy is read-only and does not modify any state.
        attempt_result = TaskAttemptPolicy.can_submit(user_id, task_id)
        if not attempt_result.allowed:
            raise SubmissionError(attempt_result.reason)

        # ── 2. Fetch task for context building ─────────────────
        task = db.get_task(task_id)
        assert task is not None  # policy already validated existence

        # ── 5. Validate actual_data structure ─────────────────
        if not isinstance(actual_data, dict):
            raise SubmissionError(
                f"actual_data must be a dict, got {type(actual_data).__name__}"
            )

        # ── 6. Reject forbidden fields in submission ──────────
        forbidden_found = FORBIDDEN_FIELDS.intersection(actual_data.keys())
        if forbidden_found:
            raise SubmissionError(
                f"Submission contains forbidden fields: "
                f"{', '.join(sorted(forbidden_found))}"
            )

        # ── 7. Read expected_data from task definition ────────
        #    expected_data is NEVER provided by the user.
        raw_task_data = task.get("task_data") or ""
        parsed_task_data: dict = {}
        if raw_task_data:
            try:
                parsed_task_data = json.loads(raw_task_data)
            except (ValueError, TypeError):
                parsed_task_data = {}

        # Extract expected from task configuration
        expected_data: dict = {}
        if "expected" in parsed_task_data:
            expected_data["expected"] = parsed_task_data["expected"]

        # ── 8. Build the combined task_data for backward compat ─
        #    task_data contains the full context for verifiers that
        #    read from task_data directly (like DeterministicTaskVerifier).
        #    actual comes from user submission, expected from task config.
        task_data_combined: dict = {}
        if "expected" in parsed_task_data:
            task_data_combined["expected"] = parsed_task_data["expected"]
        # actual comes ONLY from user submission — never from task_data
        if "actual" in actual_data:
            task_data_combined["actual"] = actual_data["actual"]

        # ── 9. Build frozen VerificationContext ───────────────
        context = VerificationContext(
            user_id=user_id,
            task_id=task_id,
            task_type=task["type"],
            expected_data=freeze_value(expected_data),
            actual_data=freeze_value(actual_data),
            task_data=freeze_value(task_data_combined),
        )

        # ── 10. Look up registered verifier ──────────────────
        verifier = get_verifier(task["type"])
        if verifier is None:
            logger.error(
                "No verifier registered for task type '%s' (task=%d)",
                task["type"], task_id,
            )
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"No verifier registered for task type '{task['type']}'",
            )

        # ── 11. Invoke verifier (never modify state) ─────────
        try:
            result = verifier.verify(context)
        except Exception as exc:
            logger.exception(
                "Verifier raised for user=%d task=%d", user_id, task_id
            )
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Verifier exception: {exc}",
            )

        # ── 12. Ensure valid result type ─────────────────────
        if not isinstance(result, VerificationResult):
            logger.error(
                "Verifier returned %s instead of VerificationResult "
                "for user=%d task=%d",
                type(result).__name__, user_id, task_id,
            )
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Verifier returned invalid type: {type(result).__name__}",
            )

        # ── 13. Log result (never complete) ──────────────────
        logger.info(
            "Submission verified: user=%d task=%d status=%s",
            user_id, task_id, result.status.value,
        )

        return result
