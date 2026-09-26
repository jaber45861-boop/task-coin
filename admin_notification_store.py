"""
Admin Notification Linkage Store (MT-ADMIN-03)
==============================================

Persistent association between a server-side operation and the
Telegram admin message that was delivered for it.  The table is the
*only* way a callback is resolved back to server-side state: untrusted
callback data (a claim id, a forged operation id) is never believed on
its own — the linkage must already exist because **this server**
created the notification.

Schema (``admin_notifications``, created by ``db.init_db``)::

    id              INTEGER PK AUTOINCREMENT
    operation_type  TEXT    -- e.g. 'manual_proof'
    operation_id    INTEGER -- e.g. the task_submissions claim id
    admin_chat_id   INTEGER -- the config.ADMINS private chat
    message_id      INTEGER -- the Telegram message carrying the buttons
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP

Idempotency: ``UNIQUE (operation_type, operation_id, admin_chat_id)``
guarantees at most ONE linkage per operation per admin chat — a
replayed submission can never create a second pending notification
linkage for the same claim.

Boundaries (this store must NOT):
    - decide claims, authorize users, or read/write wallets, ledger,
      completion or task state
    - hold any operation state in memory (persistence only)
    - send Telegram messages (delivery belongs to AdminNotifier /
      manual_proof_inbox)
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import db

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, operation_type, operation_id, admin_chat_id, message_id, created_at"
)


@dataclass(frozen=True)
class AdminNotificationRecord:
    """Immutable snapshot of one operation → Telegram message linkage."""

    id: int
    operation_type: str
    operation_id: int
    admin_chat_id: int
    message_id: int
    created_at: str


def _row_to_record(row) -> AdminNotificationRecord:
    return AdminNotificationRecord(
        id=row["id"],
        operation_type=row["operation_type"],
        operation_id=row["operation_id"],
        admin_chat_id=row["admin_chat_id"],
        message_id=row["message_id"],
        created_at=row["created_at"],
    )


class AdminNotificationStore:
    """SQLite persistence for admin notification linkages.

    All writes go through the repository's ``db.transaction()``
    (``BEGIN IMMEDIATE``) primitive; reads use ``db.get_connection()``.
    """

    @staticmethod
    def create_linkage(
        operation_type: str,
        operation_id: int,
        admin_chat_id: int,
        message_id: int,
        db_path: str | None = None,
    ) -> int | None:
        """Persist one operation → message linkage.

        Returns the new linkage id, or ``None`` when an identical
        (operation, admin chat) linkage already exists — the UNIQUE
        constraint makes duplicate notifications idempotent under
        replay/race (the loser of the race inserts nothing).
        """
        if not isinstance(operation_type, str) or not operation_type.strip():
            raise ValueError("operation_type must be a non-empty string")
        if isinstance(operation_id, bool) or not isinstance(operation_id, int):
            raise ValueError("operation_id must be an integer")
        if operation_id <= 0:
            raise ValueError("operation_id must be positive")
        if isinstance(admin_chat_id, bool) or not isinstance(admin_chat_id, int):
            raise ValueError("admin_chat_id must be an integer")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise ValueError("message_id must be an integer")
        if message_id <= 0:
            raise ValueError("message_id must be positive")

        try:
            with db.transaction(db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO admin_notifications "
                    "(operation_type, operation_id, admin_chat_id, message_id) "
                    "VALUES (?, ?, ?, ?)",
                    (operation_type, operation_id, admin_chat_id, message_id),
                )
                linkage_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate (operation, admin chat) — already linked.
            logger.info(
                "Notification linkage already exists: op=%s id=%s chat=%s",
                operation_type, operation_id, admin_chat_id,
            )
            return None

        logger.info(
            "Notification linkage created: op=%s id=%s chat=%s msg=%s "
            "linkage=%s",
            operation_type, operation_id, admin_chat_id, message_id,
            linkage_id,
        )
        return linkage_id

    @staticmethod
    def list_for_operation(
        operation_type: str,
        operation_id: int,
        db_path: str | None = None,
    ) -> list[AdminNotificationRecord]:
        """Every linkage created for one operation (one per admin chat)."""
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM admin_notifications "
                "WHERE operation_type = ? AND operation_id = ? "
                "ORDER BY id",
                (operation_type, operation_id),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    @staticmethod
    def has_linkage(
        operation_type: str,
        operation_id: int,
        db_path: str | None = None,
    ) -> bool:
        """True when at least one notification message exists for the op."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM admin_notifications "
                "WHERE operation_type = ? AND operation_id = ? LIMIT 1",
                (operation_type, operation_id),
            ).fetchone()
        return row is not None
