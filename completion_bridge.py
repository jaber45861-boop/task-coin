"""
Secure Task Completion Bridge (Micro-task 2.11)
================================================

Bridges TaskSubmissionService verification results to CompletionGate.

Flow:
    TaskAttemptPolicy
        ↓
    TaskSubmissionService.submit()
        ↓
    VerificationResult
        ↓
    ONLY if PASSED → CompletionGate.complete()
        ↓
    COMPLETED

Security:
    - FAILED → task stays STARTED, no completion attempted.
    - ERROR  → task stays STARTED, no completion attempted.
    - PASSED is the ONLY result allowed to call CompletionGate.
    - The bridge never directly updates user_tasks.
    - The bridge never imports or bypasses CompletionGate.
"""

from task_attempt import TaskAttemptPolicy
from task_submission import (
    IdempotentReplayError,
    SubmissionError,
    TaskSubmissionService,
)
from task_completion import (
    CompletionGate,
    CompletionGateError,
    VerificationResult,
    VerificationStatus,
)


class CompletionBridgeError(Exception):
    """Raised when the completion bridge encounters an unrecoverable error."""
    pass


class CompletionBridge:
    """
    Secure bridge from submission verification to task completion.

    Orchestrates:
        0. TaskSubmissionService.replay_result() — idempotent replay
           returns the ORIGINAL persisted outcome (MT-TASK-04).
        1. TaskAttemptPolicy.can_submit() — is this user allowed to attempt?
        2. TaskSubmissionService.submit() — verify the submission data.
        3. ONLY if PASSED: CompletionGate.complete() — transition to COMPLETED.

    The bridge NEVER:
        - Directly writes to user_tasks.
        - Calls CompletionGate for FAILED/ERROR results.
        - Trusts client-supplied status/completion/reward fields.
    """

    @staticmethod
    def complete_after_verification(
        user_id: int,
        task_id: int,
        actual_data: dict,
        idempotency_key: str | None = None,
    ) -> VerificationResult:
        """
        Validate, verify, and (only if passed) complete a task.

        Args:
            user_id: The Telegram user ID.
            task_id: The task ID.
            actual_data: User-provided submission data dict.
            idempotency_key: Optional submission idempotency key
                (MT-TASK-04) threaded to the SubmissionService.

        Returns:
            VerificationResult — the outcome of the full pipeline.

        Raises:
            CompletionBridgeError: On invalid inputs or unrecoverable errors.
        """
        if not isinstance(actual_data, dict):
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="actual_data must be a dict",
            )

        # 0. Idempotent replay (MT-TASK-04) — resolved BEFORE the
        #    policy gate so a repeated key returns the ORIGINAL
        #    persisted outcome even after completion made the task
        #    terminal for this user.  A failed lookup falls through
        #    to the normal pipeline.
        try:
            replay = TaskSubmissionService.replay_result(
                user_id, task_id, idempotency_key
            )
        except Exception:
            replay = None
        if replay is not None:
            return replay

        # 1. Policy gate — are we allowed to attempt?
        try:
            policy_result = TaskAttemptPolicy.can_submit(user_id, task_id)
        except Exception as exc:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Policy check failed: {exc}",
            )

        if not policy_result.allowed:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason=policy_result.reason,
            )

        # 2. Submission — verify the data (and persist the attempt)
        try:
            verification_result = TaskSubmissionService.submit(
                user_id, task_id, actual_data, idempotency_key
            )
        except IdempotentReplayError as replay:
            # Same idempotency key: return the ORIGINAL persisted
            # outcome — no second verification, no second completion.
            return replay.result
        except SubmissionError as exc:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Submission rejected: {exc}",
            )
        except Exception as exc:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"Submission failed: {exc}",
            )

        # 3. ONLY PASSED triggers completion via CompletionGate
        if verification_result.status == VerificationStatus.PASSED:
            try:
                gate = CompletionGate()
                gate.complete(user_id, task_id, verification_result)
            except CompletionGateError as exc:
                return VerificationResult(
                    status=VerificationStatus.ERROR,
                    reason=f"Completion gate rejected: {exc}",
                )
            except Exception as exc:
                return VerificationResult(
                    status=VerificationStatus.ERROR,
                    reason=f"Completion gate failed: {exc}",
                )
            # The gate transition succeeded — stamp completed_at on the
            # submission record so it identifies the completion
            # transition it produced (MT-TASK-04).  A stamping failure
            # never undoes the completed task; it is logged only.
            try:
                TaskSubmissionService.record_completion(
                    user_id, task_id, idempotency_key
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to stamp submission completion: "
                    "user=%d task=%d",
                    user_id, task_id,
                )

        # 4. Return the verification result (PASSED, FAILED, or ERROR)
        return verification_result
