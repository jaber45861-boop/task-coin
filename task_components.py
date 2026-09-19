"""
Task Lifecycle Components
-------------------------
Approved building blocks for the task lifecycle.

Each component is a single-responsibility class or enum.
TaskLifecycle (in task_lifecycle.py) orchestrates them;
this module does NOT contain orchestration logic.

Database:
    tasks       – available tasks (task_id, title, description, is_active)
    user_tasks  – per-user task state (user_id, task_id, status, timestamps)

Status flow:
    AVAILABLE → STARTED → COMPLETED
"""

from __future__ import annotations

import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Status enum ──────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"


# ── Verification result ──────────────────────────────────────────────

class VerificationResult(Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


# ── Data classes ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Task:
    task_id: int
    title: str
    description: str = ""
    is_active: bool = True


@dataclass(frozen=True)
class UserTask:
    user_id: int
    task_id: int
    status: TaskStatus
    started_at: Optional[str] = None
    submitted_at: Optional[str] = None
    verified_at: Optional[str] = None


@dataclass(frozen=True)
class StartResult:
    success: bool
    user_task: Optional[UserTask] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SubmitResult:
    success: bool
    verification_result: Optional[VerificationResult] = None
    user_task: Optional[UserTask] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class VerificationContext:
    """Immutable context passed to the verifier."""
    user_id: int
    task_id: int
    actual_data: dict[str, Any]


# ── Database helpers (thin layer over sqlite3) ───────────────────────

_DEFAULT_DB_PATH = "task_coin.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_task_db(db_path: str | None = None) -> None:
    """Create tasks and user_tasks tables if they don't exist."""
    conn = _get_conn(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_active  INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id     INTEGER NOT NULL,
                task_id     INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'AVAILABLE',
                started_at  TEXT,
                submitted_at TEXT,
                verified_at TEXT,
                PRIMARY KEY (user_id, task_id)
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ── Task data access ─────────────────────────────────────────────────

def get_task(task_id: int, *, db_path: str | None = None) -> Optional[Task]:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT task_id, title, description, is_active FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return Task(task_id=row[0], title=row[1], description=row[2], is_active=bool(row[3]))
    finally:
        conn.close()


def get_user_task(user_id: int, task_id: int, *, db_path: str | None = None) -> Optional[UserTask]:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, task_id, status, started_at, submitted_at, verified_at "
            "FROM user_tasks WHERE user_id = ? AND task_id = ?",
            (user_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return UserTask(
            user_id=row[0],
            task_id=row[1],
            status=TaskStatus(row[2]),
            started_at=row[3],
            submitted_at=row[4],
            verified_at=row[5],
        )
    finally:
        conn.close()


def user_exists(user_id: int, *, db_path: str | None = None) -> bool:
    """Check if user_id exists in user_tasks (any status)."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM user_tasks WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def user_task_exists(user_id: int, task_id: int, *, db_path: str | None = None) -> bool:
    """Check if a user_tasks row exists at all (any status)."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM user_tasks WHERE user_id = ? AND task_id = ? LIMIT 1",
            (user_id, task_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ── TaskStartGate ────────────────────────────────────────────────────

class TaskStartGate:
    """Validates and executes the start-task operation.

    Rules:
        - task must exist and be active
        - user must not have any existing user_task row for this task
          (neither STARTED nor COMPLETED)

    Does NOT grant rewards, verify anything, or interact with Telegram.
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def start(self, user_id: int, task_id: int) -> StartResult:
        task = get_task(task_id, db_path=self._db_path)
        if task is None:
            return StartResult(success=False, error="TASK_NOT_FOUND")
        if not task.is_active:
            return StartResult(success=False, error="TASK_INACTIVE")

        existing = get_user_task(user_id, task_id, db_path=self._db_path)
        if existing is not None:
            return StartResult(success=False, error="ALREADY_STARTED_OR_COMPLETED")

        now = _now_iso()
        conn = _get_conn(self._db_path)
        try:
            conn.execute(
                "INSERT INTO user_tasks (user_id, task_id, status, started_at) VALUES (?, ?, ?, ?)",
                (user_id, task_id, TaskStatus.STARTED.value, now),
            )
            conn.commit()
        finally:
            conn.close()

        ut = UserTask(
            user_id=user_id,
            task_id=task_id,
            status=TaskStatus.STARTED,
            started_at=now,
        )
        return StartResult(success=True, user_task=ut)


# ── TaskAttemptPolicy ────────────────────────────────────────────────

class TaskAttemptPolicy:
    """Guards submission attempts.

    Rules:
        - user_task row must exist
        - status must be STARTED
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def check(self, user_id: int, task_id: int) -> tuple[bool, str | None]:
        ut = get_user_task(user_id, task_id, db_path=self._db_path)
        if ut is None:
            return False, "NOT_STARTED"
        if ut.status == TaskStatus.COMPLETED:
            return False, "ALREADY_COMPLETED"
        if ut.status != TaskStatus.STARTED:
            return False, "INVALID_STATUS"
        return True, None


# ── TaskSubmissionService ────────────────────────────────────────────

FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "user_id",
    "task_id",
    "status",
    "started_at",
    "verified_at",
    "reward",
    "balance",
})


class TaskSubmissionService:
    """Validates and records a submission.

    Rules:
        - actual_data must be a dict
        - must not contain client-controlled forbidden fields
        - updates submitted_at on the user_tasks row
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def submit(self, user_id: int, task_id: int, actual_data: dict) -> tuple[bool, str | None]:
        if not isinstance(actual_data, dict):
            return False, "INVALID_DATA_TYPE"

        forbidden_found = set(actual_data.keys()) & FORBIDDEN_FIELDS
        if forbidden_found:
            return False, f"FORBIDDEN_FIELDS: {', '.join(sorted(forbidden_found))}"

        now = _now_iso()
        conn = _get_conn(self._db_path)
        try:
            conn.execute(
                "UPDATE user_tasks SET submitted_at = ? WHERE user_id = ? AND task_id = ?",
                (now, user_id, task_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True, None


# ── TaskVerifier ─────────────────────────────────────────────────────

class TaskVerifier:
    """Verifies a submission.

    Default behaviour: always returns PASSED.
    Override `verify()` in subclasses for custom logic.
    """

    def verify(self, ctx: VerificationContext) -> VerificationResult:
        """Override in subclasses for real verification."""
        return VerificationResult.PASSED


# ── CompletionGate ───────────────────────────────────────────────────

class CompletionGate:
    """Authoritative completion mechanism.

    Marks the user_task as COMPLETED and records verified_at.
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    def complete(self, user_id: int, task_id: int) -> None:
        now = _now_iso()
        conn = _get_conn(self._db_path)
        try:
            conn.execute(
                "UPDATE user_tasks SET status = ?, verified_at = ? "
                "WHERE user_id = ? AND task_id = ?",
                (TaskStatus.COMPLETED.value, now, user_id, task_id),
            )
            conn.commit()
        finally:
            conn.close()


# ── CompletionBridge ─────────────────────────────────────────────────

class CompletionBridge:
    """Thin bridge that delegates to CompletionGate.

    This is the ONLY path to mark a task as COMPLETED.
    """

    def __init__(self, gate: CompletionGate | None = None, *, db_path: str | None = None) -> None:
        self._gate = gate or CompletionGate(db_path=db_path)

    def complete(self, user_id: int, task_id: int) -> None:
        self._gate.complete(user_id, task_id)
