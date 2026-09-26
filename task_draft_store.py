"""
Task Draft Store (MT-ADMIN-05)
==============================

Durable persistence for the Telegram admin task-creation wizard.
The ``admin_task_drafts`` table (created by ``db.init_db``) is the
ONLY place wizard state lives: a draft survives bot restarts, handler
recreation and process failures between steps.  There is no in-memory
draft dict anywhere in the wizard.

Schema::

    draft_id           INTEGER PK AUTOINCREMENT
    admin_user_id      INTEGER  -- the ONE admin who owns the draft
    step               TEXT     -- current wizard step
    status             TEXT     -- 'open' | 'published'
    payload_json       TEXT     -- the wizard's collected fields
    published_task_id  INTEGER  -- tasks row created on publish
    created_at / updated_at     -- repository TIMESTAMP convention

Ownership boundary (enforced on EVERY operation):
    A draft belongs to exactly one admin.  Each read/write is filtered
    by ``admin_user_id`` — one admin can never read, edit, cancel or
    publish another admin's draft, even with a forged draft id.

Idempotency (duplicate confirm / race):
    ``claim_for_publish()`` is a compare-and-swap inside the caller's
    ``db.transaction()`` (BEGIN IMMEDIATE): only the transaction that
    flips ``status: open → published`` creates the task; a replayed or
    concurrent confirm reads ``published_task_id`` instead and creates
    nothing.  Telegram button disabling is never relied upon.

Boundaries (this store must NOT):
    - validate wizard fields or task contracts (task_taxonomy /
      task_creation own that)
    - create tasks (task_creation owns that; the store only exposes
      the CAS on the caller's connection)
    - send Telegram messages or know about callbacks
    - authorize users (it enforces draft ownership only; the wizard
      re-checks config.is_admin for every action)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

import db

logger = logging.getLogger(__name__)

DRAFT_STATUS_OPEN = "open"
DRAFT_STATUS_PUBLISHED = "published"

_COLUMNS = (
    "draft_id, admin_user_id, step, status, payload_json, "
    "published_task_id, created_at, updated_at"
)


@dataclass(frozen=True)
class TaskDraft:
    """Immutable snapshot of one admin's task-creation draft."""

    draft_id: int
    admin_user_id: int
    step: str
    status: str
    payload: dict
    published_task_id: int | None
    created_at: str
    updated_at: str

    @property
    def is_open(self) -> bool:
        return self.status == DRAFT_STATUS_OPEN


def _row_to_draft(row: sqlite3.Row | None) -> TaskDraft | None:
    """Convert a row, treating a corrupt payload as NO draft.

    A draft whose payload_json cannot be parsed is stale/broken state:
    it must fail safe (appear missing) rather than crash the wizard or
    be half-trusted.
    """
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        logger.warning(
            "Corrupt task draft payload: draft_id=%s", row["draft_id"]
        )
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "Non-object task draft payload: draft_id=%s", row["draft_id"]
        )
        return None
    return TaskDraft(
        draft_id=row["draft_id"],
        admin_user_id=row["admin_user_id"],
        step=row["step"],
        status=row["status"],
        payload=payload,
        published_task_id=row["published_task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_draft(draft_id: object) -> TaskDraft | None:
    """Fetch one draft by id (ownership is checked by the caller).

    Returns None for missing ids, non-int ids and corrupt payloads —
    every caller treats None as "safe stale failure".
    """
    if isinstance(draft_id, bool) or not isinstance(draft_id, int):
        return None
    with db.get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM admin_task_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
    return _row_to_draft(row)


def get_open_draft(admin_user_id: int) -> TaskDraft | None:
    """The admin's currently open draft, or None.

    At most one open draft per admin is possible (partial unique
    index); newest wins defensively if legacy data ever has more.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM admin_task_drafts "
            "WHERE admin_user_id = ? AND status = ? "
            "ORDER BY draft_id DESC LIMIT 1",
            (admin_user_id, DRAFT_STATUS_OPEN),
        ).fetchone()
    return _row_to_draft(row)


def get_or_create_open_draft(
    admin_user_id: int, step: str, payload: dict
) -> TaskDraft:
    """Resume the admin's open draft, or create a fresh one.

    ``db.transaction()`` (BEGIN IMMEDIATE) decides the race between
    two simultaneous /addtask invocations: exactly one INSERT wins,
    the loser resumes the row the winner just created.
    """
    with db.transaction() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM admin_task_drafts "
            "WHERE admin_user_id = ? AND status = ? "
            "ORDER BY draft_id DESC LIMIT 1",
            (admin_user_id, DRAFT_STATUS_OPEN),
        ).fetchone()
        existing = _row_to_draft(row)
        if existing is not None:
            return existing
        cursor = conn.execute(
            "INSERT INTO admin_task_drafts "
            "(admin_user_id, step, status, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (
                admin_user_id,
                step,
                DRAFT_STATUS_OPEN,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        draft_id = cursor.lastrowid
        created = conn.execute(
            f"SELECT {_COLUMNS} FROM admin_task_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
    logger.info(
        "Task draft created: draft_id=%s admin=%s step=%s",
        draft_id, admin_user_id, step,
    )
    return _row_to_draft(created)


def save_step(
    draft_id: object,
    admin_user_id: object,
    step: str,
    payload: dict,
) -> TaskDraft | None:
    """Persist one wizard step advance. Returns the saved draft.

    The UPDATE is filtered by ``draft_id`` + ``admin_user_id`` +
    ``status='open'``: a forged/foreign/stale draft id yields None and
    changes nothing.
    """
    if isinstance(draft_id, bool) or not isinstance(draft_id, int):
        return None
    if isinstance(admin_user_id, bool) or not isinstance(
        admin_user_id, int
    ):
        return None
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE admin_task_drafts "
            "SET step = ?, payload_json = ?, "
            "    updated_at = CURRENT_TIMESTAMP "
            "WHERE draft_id = ? AND admin_user_id = ? AND status = ?",
            (
                step,
                json.dumps(payload, ensure_ascii=False),
                draft_id,
                admin_user_id,
                DRAFT_STATUS_OPEN,
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM admin_task_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
    return _row_to_draft(row)


def delete_draft(draft_id: object, admin_user_id: object) -> bool:
    """Cancel a draft: delete its row. Ownership-filtered.

    Returns True only when THIS admin's open draft was removed.
    """
    if isinstance(draft_id, bool) or not isinstance(draft_id, int):
        return False
    if isinstance(admin_user_id, bool) or not isinstance(
        admin_user_id, int
    ):
        return False
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM admin_task_drafts "
            "WHERE draft_id = ? AND admin_user_id = ? AND status = ?",
            (draft_id, admin_user_id, DRAFT_STATUS_OPEN),
        )
        deleted = cursor.rowcount == 1
    if deleted:
        logger.info(
            "Task draft cancelled: draft_id=%s admin=%s",
            draft_id, admin_user_id,
        )
    return deleted


# ── Publish CAS (caller owns the transaction) ─────────────────────────
# These two helpers run on the connection yielded by the caller's
# ``db.transaction()`` so claim + task INSERT + mark land in ONE atomic
# transaction.  They never begin/commit anything themselves.


def claim_for_publish(
    conn: sqlite3.Connection, draft_id: int, admin_user_id: int
) -> bool:
    """CAS: flip the draft ``open → published``.

    Returns True for exactly ONE caller per draft; a replayed or
    concurrent confirm gets False and must NOT create a task.
    """
    cursor = conn.execute(
        "UPDATE admin_task_drafts "
        "SET status = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE draft_id = ? AND admin_user_id = ? AND status = ?",
        (
            DRAFT_STATUS_PUBLISHED,
            draft_id,
            admin_user_id,
            DRAFT_STATUS_OPEN,
        ),
    )
    return cursor.rowcount == 1


def read_published_task_id(
    conn: sqlite3.Connection, draft_id: int, admin_user_id: int
) -> int | None:
    """The task id a draft already published, or None.

    Used when ``claim_for_publish`` lost the race: the winner either
    finished (id present) or the draft vanished/foreign (None → the
    caller fails safe).
    """
    row = conn.execute(
        "SELECT published_task_id FROM admin_task_drafts "
        "WHERE draft_id = ? AND admin_user_id = ?",
        (draft_id, admin_user_id),
    ).fetchone()
    if row is None:
        return None
    return row["published_task_id"]


def mark_published(
    conn: sqlite3.Connection, draft_id: int, task_id: int
) -> None:
    """Record which task the draft created (same transaction)."""
    conn.execute(
        "UPDATE admin_task_drafts "
        "SET published_task_id = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE draft_id = ?",
        (task_id, draft_id),
    )
