"""
Task Lifecycle Orchestrator
---------------------------
Thin orchestration layer that coordinates the approved task lifecycle
components.  Contains NO business rules of its own.

    TaskStartGate → STARTED
    TaskAttemptPolicy
    TaskSubmissionService
    VerificationContext → TaskVerifier → VerificationResult
    CompletionBridge → CompletionGate → COMPLETED

Public API:
    TaskLifecycle.start_task(user_id, task_id)
    TaskLifecycle.submit_task(user_id, task_id, actual_data) -> VerificationResult
"""

from __future__ import annotations

import logging
from typing import Any

from task_components import (
    CompletionBridge,
    CompletionGate,
    StartResult,
    SubmitResult,
    TaskAttemptPolicy,
    TaskStartGate,
    TaskSubmissionService,
    TaskVerifier,
    VerificationContext,
    VerificationResult,
)

logger = logging.getLogger(__name__)


class TaskLifecycle:
    """Orchestration layer — delegates everything, owns nothing.

    This class must NOT:
        - duplicate start / attempt / submission / verifier / completion logic
        - directly modify the database
        - directly create sqlite3 connections
        - grant rewards or modify referral state
        - interact with Telegram, Mini App, HTTP, wallet, or external services
        - convert FAILED/ERROR into PASSED
        - swallow exceptions or silently retry
    """

    def __init__(
        self,
        *,
        start_gate: TaskStartGate | None = None,
        attempt_policy: TaskAttemptPolicy | None = None,
        submission_service: TaskSubmissionService | None = None,
        verifier: TaskVerifier | None = None,
        completion_bridge: CompletionBridge | None = None,
        db_path: str | None = None,
    ) -> None:
        self._start_gate = start_gate or TaskStartGate(db_path=db_path)
        self._attempt_policy = attempt_policy or TaskAttemptPolicy(db_path=db_path)
        self._submission_service = submission_service or TaskSubmissionService(db_path=db_path)
        self._verifier = verifier or TaskVerifier()
        self._completion_bridge = completion_bridge or CompletionBridge(db_path=db_path)

    # ── Public API ───────────────────────────────────────────────────

    def start_task(self, user_id: int, task_id: int) -> StartResult:
        """Start a task.  Delegates entirely to TaskStartGate.

        Returns the StartResult from TaskStartGate without modification.
        """
        return self._start_gate.start(user_id, task_id)

    def submit_task(
        self,
        user_id: int,
        task_id: int,
        actual_data: dict[str, Any],
    ) -> SubmitResult:
        """Submit a task for verification.

        Delegates through the full secure submission lifecycle:
            1. TaskAttemptPolicy  – can this user submit?
            2. TaskSubmissionService – validate data, record submission
            3. VerificationContext → TaskVerifier – verify
            4. CompletionBridge → CompletionGate – complete (only on PASSED)

        Returns SubmitResult preserving the original result/error semantics.
        """
        # 1. Attempt policy check
        allowed, reason = self._attempt_policy.check(user_id, task_id)
        if not allowed:
            return SubmitResult(success=False, error=reason)

        # 2. Submission validation + recording
        submitted, reason = self._submission_service.submit(user_id, task_id, actual_data)
        if not submitted:
            return SubmitResult(success=False, error=reason)

        # 3. Verification
        ctx = VerificationContext(
            user_id=user_id,
            task_id=task_id,
            actual_data=actual_data,
        )
        try:
            vr = self._verifier.verify(ctx)
        except Exception:
            # Verifier exceptions become ERROR — do NOT swallow
            vr = VerificationResult.ERROR

        # 4. Completion (only on PASSED)
        if vr == VerificationResult.PASSED:
            self._completion_bridge.complete(user_id, task_id)

        return SubmitResult(
            success=True,
            verification_result=vr,
        )
