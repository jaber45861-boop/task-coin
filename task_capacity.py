"""
Task Execution Capacity & Reservation Policy (MT-TASK-28)
=========================================================

Execution limits, capacity accounting and reservation expiry for
tasks.

The model
---------
A task may declare two OPTIONAL limits (both NULL = legacy task, so
every legacy task keeps exactly its current behaviour):

``max_executions``      total executions allowed over the task's life
``reservation_minutes`` how long one user may hold an unfinished start

Capacity is ACCOUNTED, never assumed::

    claimed = approved executions           (task_submissions, passed)
            + active reservations           (user_tasks, started, not expired)
            + proofs awaiting review        (task_submissions, submitted
                                             + approval_status = pending)

A start is allowed only while ``claimed < max_executions``.  Because
the count and the state change happen inside one
``db.transaction()`` (BEGIN IMMEDIATE), concurrent starts serialize:
exactly one observes the last free slot, the rest are refused.

Reservations
------------
A reservation ends in exactly one of three ways:

1. the user submits a proof in time → the submission occupies the
   slot and the reservation is no longer the limiting factor
   (``decision`` decides, never the clock);
2. the reservation EXPIRES before a proof → the slot is released and
   the cycle returns to ``available`` so someone else may take it;
3. the approver decides → approved counts forever, rejected frees the
   slot for another attempt subject to the attempt policy.

Expiry is COMPUTED from persisted server timestamps
(``user_tasks.reserved_until``), never from client input and never
from a background scheduler's own clock.  The sweeper below is only a
convenience that applies the same rule; the rule itself holds even if
the sweeper never runs, because every reader evaluates the deadline.

Nothing in this module touches a wallet, a ledger or a reward.  It
only reads task state and moves a cycle between ``started`` and
``available``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

# ── Legacy-compatibility policy (MT-TASK-28) ────────────────────────
# NULL means "not configured" and preserves legacy behaviour exactly:
#   max_executions NULL      → unlimited executions
#   reservation_minutes NULL → a start never expires
# No default number is ever invented for a legacy row.

MAX_EXECUTIONS_CEILING = 1_000_000
RESERVATION_MINUTES_CEILING = 7 * 24 * 60  # one week


def _normalize_positive_int(value: object, field: str) -> int | None:
    """Validate an optional positive-execution/minutes value.

    Returns None (meaning "not configured", i.e. legacy) or a
    positive int.  Raises ValueError for anything else — including
    booleans, floats, zero, negatives, and non-numeric text.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError(f"{field} must be a positive integer")
        value = int(text)
    if not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


def validate_max_executions(value: object) -> int | None:
    normalized = _normalize_positive_int(value, "max_executions")
    if normalized is not None and normalized > MAX_EXECUTIONS_CEILING:
        raise ValueError(
            f"max_executions cannot exceed {MAX_EXECUTIONS_CEILING}"
        )
    return normalized


def validate_reservation_minutes(value: object) -> int | None:
    normalized = _normalize_positive_int(value, "reservation_minutes")
    if (
        normalized is not None
        and normalized > RESERVATION_MINUTES_CEILING
    ):
        raise ValueError(
            "reservation_minutes cannot exceed "
            f"{RESERVATION_MINUTES_CEILING}"
        )
    return normalized


def _utcnow_text(now: datetime | None = None) -> str:
    """Server `now` in the same ISO shape ``reserved_until`` stores."""
    now = now or datetime.now(timezone.utc)
    return now.isoformat()


def _parse_instant(value: object) -> datetime | None:
    """Parse a persisted timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # SQLite's CURRENT_TIMESTAMP is UTC; treat naive values as UTC
        # rather than guessing a local offset.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reservation_deadline(task: dict, started_at: object = None) -> str | None:
    """The stored ``reserved_until`` text for a start, or None.

    Derived ONLY from the task's server-side policy and the persisted
    start instant; a legacy task (``reservation_minutes`` NULL) always
    returns None, i.e. "never expires".
    """
    minutes = task.get("reservation_minutes")
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or minutes <= 0
    ):
        return None
    started = _parse_instant(started_at)
    if started is None:
        started = datetime.now(timezone.utc)
    instant = started + timedelta(minutes=minutes)
    return instant.isoformat()


def is_reservation_expired(
    user_task: dict,
    task: dict,
    now: datetime | None = None,
) -> bool:
    """True when a STARTED cycle's reservation deadline has passed.

    Fail-safe: an unreadable or absent deadline is NOT treated as
    expired, so a corrupt row can never silently free a slot.
    """
    if user_task is None:
        return False
    if user_task.get("status") != db.USER_TASK_STATUS_STARTED:
        return False
    deadline = _parse_instant(user_task.get("reserved_until"))
    if deadline is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= deadline


def remaining_seconds(
    user_task: dict,
    task: dict,
    now: datetime | None = None,
) -> int | None:
    """Seconds left on a reservation, or None when unbounded/unknown."""
    if user_task is None:
        return None
    deadline = _parse_instant(user_task.get("reserved_until"))
    if deadline is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((deadline - now).total_seconds()))


def claimed_capacity(task_id: int, connection=None) -> int:
    """How many of a task's execution slots are already occupied.

    Counted under one connection (the caller's transaction when it has
    one) so the number and the following write cannot disagree.

    A cycle occupies a slot when ANY of these holds:

    approved        a passed submission — terminal, never frees
    awaiting_review a proof the approver has not decided yet
    active_start    a start still inside its reservation window and
                    carrying no submission of its own

    Expired reservations are NOT counted: their slot already went
    back to the pool, so counting them would shrink capacity forever.

    The three groups overlap (a start that has just submitted a proof
    is both awaiting_review and active_start), so they are unioned and
    de-duplicated by user rather than summed.  An earlier version
    summed them and subtracted, which under-counted whenever an
    awaiting row belonged to a user whose user_tasks row was not
    'started' — e.g. a completed cycle still under review.
    """
    query = (
        "SELECT COUNT(DISTINCT user_id) FROM ("
        "    SELECT user_id FROM task_submissions "
        "    WHERE task_id = ? AND status = ?"
        "    UNION"
        "    SELECT user_id FROM task_submissions "
        "    WHERE task_id = ? AND approval_status = ?"
        "    UNION"
        "    SELECT user_id FROM user_tasks "
        "    WHERE task_id = ? AND status = ?"
        "    AND (reserved_until IS NULL OR reserved_until > ?)"
        "    AND NOT EXISTS ("
        "        SELECT 1 FROM task_submissions "
        "        WHERE task_submissions.task_id = user_tasks.task_id "
        "        AND task_submissions.user_id = user_tasks.user_id"
        "    )"
        ")"
    )
    params = (
        task_id, db.SUBMISSION_STATUS_PASSED,
        task_id, db.SUBMISSION_APPROVAL_PENDING,
        task_id, db.USER_TASK_STATUS_STARTED, _utcnow_text(),
    )

    def _run(conn) -> int:
        return conn.execute(query, params).fetchone()[0]

    if connection is not None:
        return _run(connection)
    with db.transaction() as conn:
        return _run(conn)


def available_slots(task_id: int, connection=None) -> int | None:
    """Remaining slots, or None when the task is unlimited."""
    task = db.get_task(task_id)
    if task is None:
        return None
    limit = task.get("max_executions")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return None
    used = claimed_capacity(task_id, connection=connection)
    return max(0, limit - used)


class CapacityExhaustedError(Exception):
    """The task's total execution limit has been reached."""

    code = "capacity_exhausted"


class ReservationExpiredError(Exception):
    """The user's reservation expired before a proof was submitted."""

    code = "reservation_expired"


def release_expired_reservation(
    user_id: int, task_id: int, now: datetime | None = None
) -> bool:
    """Return one expired, unfinished start to the available pool.

    Released only when the cycle is still ``started``, its deadline
    has passed, AND no proof was ever submitted for it — a submitted
    proof keeps its slot until the reviewer decides.  The comparison
    and the write are one guarded statement, so a concurrent
    submission cannot be undone by an expiry sweep.
    """
    now = now or datetime.now(timezone.utc)
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE user_tasks SET status = ?, "
            "started_at = NULL, reserved_until = NULL "
            "WHERE user_id = ? AND task_id = ? AND status = ? "
            "AND reserved_until IS NOT NULL AND reserved_until <= ? "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM task_submissions "
            "    WHERE user_id = ? AND task_id = ?"
            ")",
            (
                db.USER_TASK_STATUS_AVAILABLE,
                user_id,
                task_id,
                db.USER_TASK_STATUS_STARTED,
                _utcnow_text(now),
                user_id,
                task_id,
            ),
        )
        return cursor.rowcount == 1


def find_expired_reservations(limit: int = 100) -> list[tuple[int, int]]:
    """(user_id, task_id) pairs whose reservation has lapsed unused."""
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT user_id, task_id FROM user_tasks "
            "WHERE status = ? AND reserved_until IS NOT NULL "
            "AND reserved_until <= ? "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM task_submissions "
            "    WHERE task_submissions.user_id = user_tasks.user_id "
            "    AND task_submissions.task_id = user_tasks.task_id"
            ") ORDER BY reserved_until LIMIT ?",
            (db.USER_TASK_STATUS_STARTED, _utcnow_text(), limit),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]


def sweep_expired_reservations(limit: int = 100) -> int:
    """Release every lapsed reservation (best effort, idempotent)."""
    released = 0
    for user_id, task_id in find_expired_reservations(limit):
        try:
            if release_expired_reservation(user_id, task_id):
                released += 1
                logger.info(
                    "Expired task reservation released: user=%s task=%s",
                    user_id,
                    task_id,
                )
        except Exception:
            logger.exception(
                "Reservation release failed: user=%s task=%s",
                user_id,
                task_id,
            )
    return released
