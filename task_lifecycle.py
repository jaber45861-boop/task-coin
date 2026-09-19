"""
Task Lifecycle Orchestrator
---------------------------
Thin orchestration layer that coordinates the approved task lifecycle
components.  Contains NO business rules of its own.

    TaskStartGate.start()
        ↓
    STARTED
        ↓
    CompletionBridge.complete_after_verification()
        ↓
    TaskAttemptPolicy → TaskSubmissionService → VerificationResult
        ↓  (only if PASSED)
    CompletionGate → COMPLETED

Public API:
    TaskLifecycle.start_task(user_id, task_id) -> StartResult
    TaskLifecycle.submit_task(user_id, task_id, actual_data) -> VerificationResult
"""

from __future__ import annotations

import logging

from task_start import TaskStartGate, StartResult, StartGateError
from completion_bridge import CompletionBridge, CompletionBridgeError
from task_completion import VerificationResult, VerificationStatus

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

    def __init__(self) -> None:
        self._start_gate = TaskStartGate()

    # ── Public API ───────────────────────────────────────────────────

    def start_task(self, user_id: int, task_id: int) -> StartResult:
        """Start a task.  Delegates entirely to TaskStartGate.

        Returns the StartResult from TaskStartGate without modification.
        Raises StartGateError on failure (preserving existing semantics).
        """
        return self._start_gate.start(user_id, task_id)

    def submit_task(
        self,
        user_id: int,
        task_id: int,
        actual_data: dict,
    ) -> VerificationResult:
        """Submit a task for verification.

        Delegates through the full secure submission lifecycle via
        CompletionBridge.complete_after_verification():

            1. TaskAttemptPolicy  – can this user submit?
            2. TaskSubmissionService – validate data, record submission
            3. VerificationContext → TaskVerifier – verify
            4. CompletionBridge → CompletionGate – complete (only on PASSED)

        Returns VerificationResult preserving the original result semantics.
        """
        return CompletionBridge.complete_after_verification(
            user_id, task_id, actual_data
        )
