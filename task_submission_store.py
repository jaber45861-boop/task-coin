"""
Task Submission Store (MT-TASK-04)
==================================

Database persistence and retrieval for task submission/attempt records.

This module is the ONLY place that reads or writes the ``task_submissions``
table.  It contains no verification, no completion and no policy logic —
only record CRUD built on the existing ``db.transaction()`` (BEGIN
IMMEDIATE) primitive.

Record semantics:

- ``(user_id, task_id)`` is the user_tasks identity (composite PK), so a
  submission record is always tied to exactly one user/task cycle context.
- ``attempt_number`` is the 1-based ordinal of the user's attempts for
  that task — computed inside the write transaction so concurrent
  attempts serialize and never share a number.
- ``status`` moves submitted → passed | failed | error (terminal).  A
  failed/error attempt is never deleted: the table is an audit trail.
- ``idempotency_key`` is enforced by the database:
  ``UNIQUE (user_id, task_id, idempotency_key)`` — the same key can never
  create a second record, and a key is only ever matched inside its own
  (user, task) context.
- ``completed_at`` is stamped only on the passed submission whose
  verification led to the CompletionGate transition.

Never persisted: tokens, client payloads, reward values, task_data,
verification internals beyond the verifier's own reason string (kept
server-side for audit only — never surfaced to the client by routes).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

import db

logger = logging.getLogger(__name__)

# How long an in-flight ('submitted') claim is awaited before the caller
# is told the attempt is still processing.  Reads only — no lock is held.
IN_FLIGHT_WAIT_SECONDS = 2.0
IN_FLIGHT_POLL_INTERVAL = 0.05

_TERMINAL_STATUSES = frozenset({
    db.SUBMISSION_STATUS_PASSED,
    db.SUBMISSION_STATUS_FAILED,
    db.SUBMISSION_STATUS_ERROR,
})


@dataclass(frozen=True)
class SubmissionRecord:
    """Immutable snapshot of one submission/attempt record."""

    submission_id: int
    user_id: int
    task_id: int
    attempt_number: int
    status: str
    idempotency_key: str
    verification_reason: str | None
    submitted_at: str
    completed_at: str | None
    created_at: str
    # Buyer-approval state (MT-TASK-06): NULL for every non-referral
    # submission; 'pending' | 'approved' | 'rejected' for an
    # approval-gated referral claim.
    approval_status: str | None = None
    approver_user_id: int | None = None
    approval_decided_at: str | None = None
    # Manual/social-proof reference (MT-TASK-15): NULL for every
    # non-manual submission; the bounded text/URL proof reference of a
    # manual proof claim.  Presentation-only — never identity,
    # authorization, reward or task data.
    proof_ref: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def _row_to_record(row) -> SubmissionRecord:
    return SubmissionRecord(
        submission_id=row["submission_id"],
        user_id=row["user_id"],
        task_id=row["task_id"],
        attempt_number=row["attempt_number"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        verification_reason=row["verification_reason"],
        submitted_at=row["submitted_at"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
        approval_status=row["approval_status"],
        approver_user_id=row["approver_user_id"],
        approval_decided_at=row["approval_decided_at"],
        proof_ref=row["proof_ref"],
    )


_COLUMNS = (
    "submission_id, user_id, task_id, attempt_number, status, "
    "idempotency_key, verification_reason, submitted_at, completed_at, "
    "created_at, approval_status, approver_user_id, approval_decided_at, "
    "proof_ref"
)


class TaskSubmissionStore:
    """Record persistence for task submissions.

    This class must NOT:
        - run verifiers or decide verification outcomes
        - touch user_tasks, users, tasks, wallets or ledger
        - implement repeat/attempt policy rules
    """

    # ── Create / reuse (idempotency) ──────────────────────────────

    @staticmethod
    def create_submission(
        user_id: int,
        task_id: int,
        idempotency_key: str,
        db_path: str | None = None,
    ) -> tuple[SubmissionRecord, bool]:
        """Create a 'submitted' claim, or return the existing record.

        Returns ``(record, created)``:
        - ``created=True``: a new attempt was claimed by this caller and
          it is responsible for verifying and recording the result.
        - ``created=False``: a record already exists for
          ``(user_id, task_id, idempotency_key)`` — the caller must NOT
          verify again.

        The INSERT runs inside ``db.transaction()`` (BEGIN IMMEDIATE):
        concurrent same-key claims serialize and the UNIQUE constraint
        decides the single winner — not application-level SELECT-then-
        INSERT logic.
        """
        with db.transaction(db_path) as conn:
            attempt_number = conn.execute(
                "SELECT COUNT(*) AS c FROM task_submissions "
                "WHERE user_id = ? AND task_id = ?",
                (user_id, task_id),
            ).fetchone()["c"] + 1
            try:
                cursor = conn.execute(
                    "INSERT INTO task_submissions "
                    "(user_id, task_id, attempt_number, status, "
                    " idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        user_id,
                        task_id,
                        attempt_number,
                        db.SUBMISSION_STATUS_SUBMITTED,
                        idempotency_key,
                    ),
                )
            except sqlite3.IntegrityError:
                # Database-enforced uniqueness: the key already exists
                # for this exact (user, task) context.
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM task_submissions "
                    "WHERE user_id = ? AND task_id = ? "
                    "AND idempotency_key = ?",
                    (user_id, task_id, idempotency_key),
                ).fetchone()
                if row is None:
                    # Unique violation from a foreign key/constraint we
                    # cannot attribute — surface safely.
                    raise
                return _row_to_record(row), False
            submission_id = cursor.lastrowid

        row = TaskSubmissionStore.get_submission(
            submission_id, db_path=db_path
        )
        logger.info(
            "Submission claimed: user=%d task=%d attempt=%d key=%s",
            user_id, task_id, attempt_number, idempotency_key,
        )
        return row, True

    # ── Result persistence ────────────────────────────────────────

    @staticmethod
    def record_verification_result(
        submission_id: int,
        status: str,
        verification_reason: str | None = None,
        db_path: str | None = None,
    ) -> SubmissionRecord | None:
        """Persist a terminal verification outcome (CAS on 'submitted').

        Only a record still in 'submitted' state transitions; a second
        writer can never overwrite an existing outcome.  Returns the
        updated record, or ``None`` when the record was not found or
        already terminal.
        """
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"invalid submission status: {status!r}")
        with db.transaction(db_path) as conn:
            cursor = conn.execute(
                "UPDATE task_submissions "
                "SET status = ?, verification_reason = ? "
                "WHERE submission_id = ? AND status = ?",
                (
                    status,
                    verification_reason,
                    submission_id,
                    db.SUBMISSION_STATUS_SUBMITTED,
                ),
            )
            if cursor.rowcount != 1:
                return None
        logger.info(
            "Submission result recorded: submission=%d status=%s",
            submission_id, status,
        )
        return TaskSubmissionStore.get_submission(
            submission_id, db_path=db_path
        )

    @staticmethod
    def stamp_completion(
        user_id: int,
        task_id: int,
        idempotency_key: str | None = None,
        db_path: str | None = None,
    ) -> bool:
        """Stamp completed_at on the passed submission that completed.

        Called by the orchestration layer ONLY after the CompletionGate
        transition succeeded, so ``completed_at`` identifies the exact
        completion transition that resulted from this submission.  With
        an idempotency key the target is exact; without one (legacy
        callers) the latest unmarked passed submission of the user/task
        is stamped.
        """
        with db.transaction(db_path) as conn:
            if idempotency_key is not None:
                cursor = conn.execute(
                    "UPDATE task_submissions "
                    "SET completed_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ? AND task_id = ? "
                    "AND idempotency_key = ? AND status = ? "
                    "AND completed_at IS NULL",
                    (
                        user_id, task_id, idempotency_key,
                        db.SUBMISSION_STATUS_PASSED,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE task_submissions "
                    "SET completed_at = CURRENT_TIMESTAMP "
                    "WHERE submission_id = ("
                    "    SELECT submission_id FROM task_submissions "
                    "    WHERE user_id = ? AND task_id = ? "
                    "    AND status = ? AND completed_at IS NULL "
                    "    ORDER BY submission_id DESC LIMIT 1"
                    ")",
                    (user_id, task_id, db.SUBMISSION_STATUS_PASSED),
                )
            return cursor.rowcount == 1

    # ── Retrieval ─────────────────────────────────────────────────

    @staticmethod
    def get_submission(
        submission_id: int, db_path: str | None = None
    ) -> SubmissionRecord | None:
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return _row_to_record(row) if row else None

    @staticmethod
    def get_by_idempotency_key(
        user_id: int,
        task_id: int,
        idempotency_key: str,
        db_path: str | None = None,
    ) -> SubmissionRecord | None:
        """Exact-context lookup — a key is only ever matched inside its
        own ``(user_id, task_id)`` scope; another user's or task's use
        of the same key string can never return this record."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? AND idempotency_key = ?",
                (user_id, task_id, idempotency_key),
            ).fetchone()
        return _row_to_record(row) if row else None

    @staticmethod
    def list_user_task_submissions(
        user_id: int,
        task_id: int,
        db_path: str | None = None,
    ) -> list[SubmissionRecord]:
        """Full attempt history for one user/task, oldest first."""
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? "
                "ORDER BY submission_id ASC",
                (user_id, task_id),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    @staticmethod
    def list_user_submissions(
        user_id: int, db_path: str | None = None
    ) -> list[SubmissionRecord]:
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? ORDER BY submission_id ASC",
                (user_id,),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    @staticmethod
    def count_attempts(
        user_id: int, task_id: int, db_path: str | None = None
    ) -> int:
        with db.get_connection(db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM task_submissions "
                "WHERE user_id = ? AND task_id = ?",
                (user_id, task_id),
            ).fetchone()["c"]

    # ── Buyer-approval claims (MT-TASK-06) ─────────────────────
    #
    # A paid-referral claim IS a submission record that is born in
    # approval_status='pending'.  These methods are the only writers of
    # that state; they never touch user_tasks, wallets, ledgers or the
    # four-state submission `status` vocabulary.

    @staticmethod
    def create_approval_claim(
        user_id: int,
        task_id: int,
        idempotency_key: str,
        db_path: str | None = None,
        proof_ref: str | None = None,
    ) -> tuple["SubmissionRecord", bool]:
        """Create a submission already marked approval-pending, or
        return the existing claim.

        ``proof_ref`` (MT-TASK-15) is the caller-validated bounded
        proof reference for a manual proof claim; NULL for every
        other claim (referral calls omit it — unchanged behavior).

        Returns ``(record, created)``:
        - ``created=True``: this caller opened the claim.
        - ``created=False``: either the same idempotency key already
          exists, or the (user, task) already has an open pending
          claim — the returned record is that claim (duplicate-claim
          prevention: at most ONE pending claim per user/task ever).

        The same-key race is decided by the UNIQUE constraint and the
        single-open-claim rule is decided inside ``BEGIN IMMEDIATE`` —
        never application-level SELECT-then-INSERT.
        """
        with db.transaction(db_path) as conn:
            existing_key = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? AND idempotency_key = ?",
                (user_id, task_id, idempotency_key),
            ).fetchone()
            if existing_key is not None:
                return _row_to_record(existing_key), False

            pending = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? "
                "AND approval_status = ? "
                "ORDER BY submission_id DESC LIMIT 1",
                (
                    user_id, task_id,
                    db.SUBMISSION_APPROVAL_PENDING,
                ),
            ).fetchone()
            if pending is not None:
                return _row_to_record(pending), False

            attempt_number = conn.execute(
                "SELECT COUNT(*) AS c FROM task_submissions "
                "WHERE user_id = ? AND task_id = ?",
                (user_id, task_id),
            ).fetchone()["c"] + 1
            try:
                cursor = conn.execute(
                    "INSERT INTO task_submissions "
                    "(user_id, task_id, attempt_number, status, "
                    " idempotency_key, approval_status, proof_ref) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id, task_id, attempt_number,
                        db.SUBMISSION_STATUS_SUBMITTED,
                        idempotency_key,
                        db.SUBMISSION_APPROVAL_PENDING,
                        proof_ref,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM task_submissions "
                    "WHERE user_id = ? AND task_id = ? "
                    "AND idempotency_key = ?",
                    (user_id, task_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
                return _row_to_record(row), False
            submission_id = cursor.lastrowid

        logger.info(
            "Approval claim opened: user=%d task=%d attempt=%d key=%s",
            user_id, task_id, attempt_number, idempotency_key,
        )
        record = TaskSubmissionStore.get_submission(submission_id, db_path)
        assert record is not None
        return record, True

    @staticmethod
    def get_pending_claim(
        user_id: int, task_id: int, db_path: str | None = None
    ) -> "SubmissionRecord | None":
        """The user's open pending claim for this task, or None."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? "
                "AND approval_status = ? "
                "ORDER BY submission_id DESC LIMIT 1",
                (user_id, task_id, db.SUBMISSION_APPROVAL_PENDING),
            ).fetchone()
        return _row_to_record(row) if row else None

    @staticmethod
    def get_latest_claim(
        user_id: int, task_id: int, db_path: str | None = None
    ) -> "SubmissionRecord | None":
        """The user's most recent approval-gated claim (any state)."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE user_id = ? AND task_id = ? "
                "AND approval_status IS NOT NULL "
                "ORDER BY submission_id DESC LIMIT 1",
                (user_id, task_id),
            ).fetchone()
        return _row_to_record(row) if row else None

    @staticmethod
    def list_pending_claims_for_task(
        task_id: int, db_path: str | None = None
    ) -> list["SubmissionRecord"]:
        """All open pending claims of one task, oldest first.
        (Approver-facing read; scoped by the caller's authorization.)"""
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM task_submissions "
                "WHERE task_id = ? AND approval_status = ? "
                "ORDER BY submission_id ASC",
                (task_id, db.SUBMISSION_APPROVAL_PENDING),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    @staticmethod
    def mark_approval_decision(
        submission_id: int,
        approval_status: str,
        approver_user_id: int,
        db_path: str | None = None,
    ) -> "SubmissionRecord | None":
        """CAS a pending claim to approved/rejected, recording who and when.

        Only a record still in approval_status='pending' transitions;
        a second (or opposite) decision can never overwrite the first.
        Returns the updated record, or None when it was not pending.
        """
        if approval_status not in (
            db.SUBMISSION_APPROVAL_APPROVED,
            db.SUBMISSION_APPROVAL_REJECTED,
        ):
            raise ValueError(f"invalid approval decision: {approval_status!r}")
        with db.transaction(db_path) as conn:
            cursor = conn.execute(
                "UPDATE task_submissions "
                "SET approval_status = ?, approver_user_id = ?, "
                "    approval_decided_at = CURRENT_TIMESTAMP "
                "WHERE submission_id = ? AND approval_status = ?",
                (
                    approval_status, approver_user_id, submission_id,
                    db.SUBMISSION_APPROVAL_PENDING,
                ),
            )
            if cursor.rowcount != 1:
                return None
        logger.info(
            "Approval decision recorded: submission=%d status=%s "
            "approver=%d",
            submission_id, approval_status, approver_user_id,
        )
        return TaskSubmissionStore.get_submission(submission_id, db_path)

    @staticmethod
    def await_terminal(
        submission_id: int,
        timeout: float = IN_FLIGHT_WAIT_SECONDS,
        poll_interval: float = IN_FLIGHT_POLL_INTERVAL,
        db_path: str | None = None,
    ) -> SubmissionRecord:
        """Wait briefly for an in-flight claim to reach a terminal state.

        Used when a concurrent request holds the same idempotency key:
        the loser never runs a second verification — it waits for the
        winner's persisted result.  Times out with the record still in
        'submitted' state so the caller can report "in progress".
        """
        deadline = time.monotonic() + timeout
        record = TaskSubmissionStore.get_submission(submission_id, db_path)
        while record is not None and not record.is_terminal:
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval)
            record = TaskSubmissionStore.get_submission(
                submission_id, db_path
            )
        return record
