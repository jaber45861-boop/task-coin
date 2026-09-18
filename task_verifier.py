"""
Task Verification Contract
==========================

Defines the internal verification interface that produces
VerificationResult objects consumed by CompletionGate.

Flow:
    Task  →  Verification  →  VerificationResult  →  CompletionGate

No verifier should call CompletionGate.complete() directly.
No verifier should modify users, tasks, user_tasks, balances, or referrals.

Usage:
    # Register a verifier for a task type
    register_verifier("subscribe", MySubscribeVerifier())

    # Verify a task (returns VerificationResult)
    result = verify_task(user_id=123, task_id=1)

    # Feed result to CompletionGate
    gate = CompletionGate()
    gate.complete(user_id=123, task_id=1, result)
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import db
from task_completion import VerificationResult, VerificationStatus

logger = logging.getLogger(__name__)


# ── Verification Context ──────────────────────────────────────────


@dataclass(frozen=True)
class VerificationContext:
    """Immutable context passed to a verifier.

    Contains all information needed to verify a task completion.
    Verifiers receive this and return a VerificationResult.

    Attributes:
        user_id:    Telegram user ID.
        task_id:    Task definition ID.
        task_type:  The task type string (e.g. "subscribe", "visit").
        task_data:  Arbitrary task metadata from the tasks table.
    """

    user_id: int
    task_id: int
    task_type: str
    task_data: dict[str, Any] = field(default_factory=dict)


# ── Verifier Protocol ────────────────────────────────────────────


class TaskVerifier(ABC):
    """Base class for task verifiers.

    Each task type should have a corresponding verifier implementation.
    The verifier:
    - Receives a VerificationContext
    - Returns a VerificationResult
    - Does NOT modify any database state
    - Does NOT call CompletionGate.complete()
    """

    @abstractmethod
    def verify(self, context: VerificationContext) -> VerificationResult:
        """Verify whether a task has been completed.

        Args:
            context: The verification context containing user/task info.

        Returns:
            VerificationResult with status PASSED, FAILED, or ERROR.
        """
        ...


# ── Verifier Registry ────────────────────────────────────────────

_verifier_registry: dict[str, TaskVerifier] = {}


def register_verifier(task_type: str, verifier: TaskVerifier) -> None:
    """Register a verifier for a task type.

    Args:
        task_type: The task type string this verifier handles.
        verifier:  A TaskVerifier implementation.

    Raises:
        TypeError: If verifier is not a TaskVerifier subclass.
    """
    if not isinstance(verifier, TaskVerifier):
        raise TypeError(
            f"verifier must be a TaskVerifier subclass, got {type(verifier).__name__}"
        )
    _verifier_registry[task_type] = verifier
    logger.debug("Registered verifier for task type: %s", task_type)


def get_verifier(task_type: str) -> TaskVerifier | None:
    """Get the verifier for a task type, or None if not registered."""
    return _verifier_registry.get(task_type)


def clear_verifiers() -> None:
    """Clear all registered verifiers. Use in test teardown only."""
    _verifier_registry.clear()


# ── Deterministic Task Verifier ───────────────────────────────────


class DeterministicTaskVerifier(TaskVerifier):
    """Verifies a task by comparing an explicit expected value against
    a supplied value in task_data.

    Verification rule:
        - task_data must contain both 'expected' and 'actual' keys.
        - If both are present, same type, and ``actual == expected`` → PASSED.
        - If both are present but differ in value or type → FAILED.
        - If either key is missing or task_data is malformed → ERROR.

    Security:
        - Truthy/falsy shortcuts (True, 1, "yes", etc.) are NOT accepted.
        - The comparison is type-strict (``type(a) is type(b) and a == b``).
        - Extra unrelated keys do not bypass the check.
    """

    def verify(self, context: VerificationContext) -> VerificationResult:
        task_data = context.task_data

        # 1. task_data must be a dict
        if not isinstance(task_data, dict):
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="task_data is not a dict",
            )

        # 2. Both 'expected' and 'actual' must be present
        if "expected" not in task_data:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="missing 'expected' in task_data",
            )
        if "actual" not in task_data:
            return VerificationResult(
                status=VerificationStatus.ERROR,
                reason="missing 'actual' in task_data",
            )

        expected = task_data["expected"]
        actual = task_data["actual"]

        # 3. Type-strict exact comparison (no truthy/falsy shortcuts)
        if type(actual) is type(expected) and actual == expected:
            return VerificationResult(status=VerificationStatus.PASSED)
        else:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                reason=f"expected {expected!r} ({type(expected).__name__}), "
                       f"got {actual!r} ({type(actual).__name__})",
            )


# Register the deterministic verifier
register_verifier("deterministic", DeterministicTaskVerifier())


# ── Main Entry Point ─────────────────────────────────────────────


def verify_task(user_id: int, task_id: int) -> VerificationResult:
    """Look up the task, build context, and run the appropriate verifier.

    This is the main entry point for verification. It:
    1. Validates user and task exist.
    2. Finds the registered verifier for the task type.
    3. Builds a VerificationContext and calls the verifier.
    4. Returns the VerificationResult (does NOT call CompletionGate).

    Args:
        user_id: Telegram user ID.
        task_id: Task definition ID.

    Returns:
        VerificationResult from the registered verifier.
        Returns ERROR result (never raises) for validation failures.
    """
    # 1. Validate user exists
    user = db.get_user(user_id)
    if user is None:
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=f"User {user_id} not found",
        )

    # 2. Validate task exists
    task = db.get_task(task_id)
    if task is None:
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=f"Task {task_id} not found",
        )

    # 3. Check verifier is registered for this task type
    verifier = _verifier_registry.get(task["type"])
    if verifier is None:
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=f"No verifier registered for task type '{task['type']}'",
        )

    # 4. Build context and verify
    # Parse task_data JSON if present, otherwise empty dict
    raw_task_data = task.get("task_data")
    parsed_task_data: dict[str, Any] = {}
    if raw_task_data:
        try:
            parsed_task_data = json.loads(raw_task_data)
        except (ValueError, TypeError):
            parsed_task_data = {}

    # Merge task metadata + task-type-specific data into context
    context = VerificationContext(
        user_id=user_id,
        task_id=task_id,
        task_type=task["type"],
        task_data={
            "title": task["title"],
            "description": task["description"],
            "reward": task["reward"],
            **parsed_task_data,
        },
    )

    try:
        result = verifier.verify(context)
    except Exception as exc:
        logger.exception("Verifier raised for user=%d task=%d", user_id, task_id)
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=f"Verifier exception: {exc}",
        )

    # Ensure the result is a valid VerificationResult
    if not isinstance(result, VerificationResult):
        logger.error(
            "Verifier returned %s instead of VerificationResult for user=%d task=%d",
            type(result).__name__,
            user_id,
            task_id,
        )
        return VerificationResult(
            status=VerificationStatus.ERROR,
            reason=f"Verifier returned invalid type: {type(result).__name__}",
        )

    return result
