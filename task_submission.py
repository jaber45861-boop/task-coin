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
import uuid
from dataclasses import dataclass

import db
from task_attempt import TaskAttemptPolicy
from task_submission_store import TaskSubmissionStore
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


# Reason marker for an idempotency key whose original attempt is still
# in flight (a concurrent request is verifying it right now).
IN_PROGRESS_REASON = "submission already in progress for this idempotency key"

# Maximum accepted length for a client-supplied idempotency key.
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class IdempotentReplayError(Exception):
    """Raised when the idempotency key already has a persisted outcome.

    Carries the ORIGINAL VerificationResult so orchestration returns
    the first attempt's result without invoking the verifier (or the
    completion gate) a second time.
    """

    def __init__(self, result: VerificationResult) -> None:
        super().__init__("idempotent replay of an existing submission")
        self.result = result


def _normalize_idempotency_key(idempotency_key: str | None) -> str:
    """Validate a caller-provided key or generate a server-side one.

    Every submission ends up with exactly one key: server-generated
    when the client does not supply one.  Invalid keys are rejected —
    never silently repaired.
    """
    if idempotency_key is None:
        return uuid.uuid4().hex
    if not isinstance(idempotency_key, str):
        raise SubmissionError("invalid idempotency key")
    key = idempotency_key.strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise SubmissionError("invalid idempotency key")
    if not all(c.isalnum() or c in "-_.,:" for c in key):
        raise SubmissionError("invalid idempotency key")
    return key


def _result_from_record(record) -> VerificationResult:
    """Rebuild a persisted submission outcome as a VerificationResult
    (used for idempotent replays — never re-runs verification)."""
    if record.status == db.SUBMISSION_STATUS_PASSED:
        return VerificationResult(status=VerificationStatus.PASSED)
    if record.status == db.SUBMISSION_STATUS_FAILED:
        return VerificationResult(
            status=VerificationStatus.FAILED,
            reason=record.verification_reason or "",
        )
    if record.status == db.SUBMISSION_STATUS_ERROR:
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=record.verification_reason or "",
        )
    # Still 'submitted': the original attempt never finished.
    return VerificationResult(
        status=VerificationStatus.ERROR,
        reason=IN_PROGRESS_REASON,
    )


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
        idempotency_key: str | None = None,
    ) -> VerificationResult:
        """Submit actual verification data for a STARTED task.

        Args:
            user_id:    Telegram user ID.
            task_id:    Task definition ID.
            actual_data: User-provided verification data (dict).
            idempotency_key: Optional client key; a server-side key is
                generated when absent.  Enforced by the database via
                UNIQUE (user_id, task_id, idempotency_key).

        Returns:
            VerificationResult with status PASSED, FAILED, or ERROR.

        Raises:
            SubmissionError: If pre-validation fails (user/task
                not found, task inactive, task not started, invalid
                payload, invalid idempotency key, etc.).
            IdempotentReplayError: When the key already has a record —
                carries the ORIGINAL persisted result; the verifier is
                NOT invoked again.
        """
        key = _normalize_idempotency_key(idempotency_key)

        # ── 0. Idempotent replay (MT-TASK-04) ─────────────────
        #    Resolved BEFORE the policy gate: a repeated key returns
        #    the ORIGINAL persisted outcome even after completion
        #    made the task terminal for this user.
        replay = TaskSubmissionService.replay_result(user_id, task_id, key)
        if replay is not None:
            raise IdempotentReplayError(replay)

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

        # ── 6b. Claim a submission record (MT-TASK-04) ────────
        #    Database-enforced idempotency: the UNIQUE constraint
        #    decides the single winner of a same-key race — this is
        #    not SELECT-then-INSERT logic.  Invalid payloads never
        #    reach this point, so no orphan records exist for them.
        record, created = TaskSubmissionStore.create_submission(
            user_id, task_id, key
        )
        if not created:
            # Idempotent replay: never run the verifier twice.
            if not record.is_terminal:
                record = TaskSubmissionStore.await_terminal(
                    record.submission_id
                )
            raise IdempotentReplayError(_result_from_record(record))

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
            result = VerificationResult(
                status=VerificationStatus.ERROR,
                reason=f"No verifier registered for task type '{task['type']}'",
            )
        else:
            # ── 11. Invoke verifier (never modify state) ─────
            try:
                result = verifier.verify(context)
            except Exception as exc:
                logger.exception(
                    "Verifier raised for user=%d task=%d", user_id, task_id
                )
                result = VerificationResult(
                    status=VerificationStatus.ERROR,
                    reason=f"Verifier exception: {exc}",
                )

            # ── 12. Ensure valid result type ─────────────────
            if not isinstance(result, VerificationResult):
                logger.error(
                    "Verifier returned %s instead of VerificationResult "
                    "for user=%d task=%d",
                    type(result).__name__, user_id, task_id,
                )
                result = VerificationResult(
                    status=VerificationStatus.ERROR,
                    reason=f"Verifier returned invalid type: {type(result).__name__}",
                )

        # ── 13. Persist the verification outcome (MT-TASK-04) ─
        #    The SubmissionService owns submission persistence: every
        #    attempt — passed, failed or error — is recorded.  The
        #    verifier stays side-effect free and the completion gate is
        #    never called from here.
        TaskSubmissionStore.record_verification_result(
            record.submission_id,
            result.status.value,
            result.reason or None,
        )

        # ── 14. Log result (never complete) ──────────────────
        logger.info(
            "Submission verified: user=%d task=%d status=%s",
            user_id, task_id, result.status.value,
        )

        return result

    # ── Idempotent replay (MT-TASK-04) ────────────────────────────

    @staticmethod
    def replay_result(
        user_id: int,
        task_id: int,
        idempotency_key: str | None,
    ) -> VerificationResult | None:
        """Return the persisted outcome for a key, or None if absent.

        Called by the orchestration layer BEFORE the policy/verification
        pipeline so a repeated request with the same key returns the
        ORIGINAL result even after completion made the task terminal.
        An in-flight claim is awaited briefly; if it never finishes the
        returned result reports the in-progress state safely.
        """
        if not idempotency_key or not isinstance(idempotency_key, str):
            return None
        record = TaskSubmissionStore.get_by_idempotency_key(
            user_id, task_id, idempotency_key.strip()
        )
        if record is None:
            return None
        if not record.is_terminal:
            record = TaskSubmissionStore.await_terminal(
                record.submission_id
            )
            if not record.is_terminal:
                return VerificationResult(
                    status=VerificationStatus.ERROR,
                    reason=IN_PROGRESS_REASON,
                )
        return _result_from_record(record)

    # ── Completion stamping (MT-TASK-04) ──────────────────────────

    @staticmethod
    def record_completion(
        user_id: int,
        task_id: int,
        idempotency_key: str | None = None,
    ) -> bool:
        """Stamp completed_at on the passed submission, AFTER the
        completion gate transition succeeded (called by orchestration).

        This is what ties a successful submission to the exact
        completion transition it produced.  Persistence stays owned by
        this service — the bridge/lifecycle only orchestrate.
        """
        return TaskSubmissionStore.stamp_completion(
            user_id, task_id, idempotency_key
        )
