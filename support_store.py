"""
Support Store (MT-ADMIN-06)
===========================

Durable persistence for the Telegram user support system.  The four
tables created by ``db.init_db`` (``support_inquiries``,
``support_messages``, ``support_user_states``,
``support_reply_contexts``) are the ONLY places support state lives:
an inquiry, its full conversation, the user's in-progress category
selection and the admin's reply context all survive bot restarts,
handler recreation and process failures.  There is no in-memory
conversation dict anywhere in this system.

Schema (created by ``db.init_db``)::

    support_inquiries
        id / user_id / category / status / created_at / updated_at
        status            'open' (active) | 'closed' (terminal)
        UNIQUE partial index: at most ONE active inquiry per user
                          (DB-enforced, not just an application check)

    support_messages
        id / inquiry_id / sender_type / sender_id / message
        source_message_id / delivered_at / created_at
        sender_type       'user' | 'admin'
        source_message_id Telegram message id of the sender's private
                          chat; the UNIQUE (sender_type, sender_id,
                          source_message_id) index makes a replayed
                          update append nothing twice
        delivered_at      admin→user delivery marker — NULL means the
                          message is preserved but NOT delivered yet

    support_user_states
        user_id (PK) / category / updated_at
        the category the user picked and is about to describe

    support_reply_contexts
        admin_user_id (PK) / admin_chat_id / inquiry_id / timestamps
        which inquiry an admin is currently answering; resolved from
        the DB by admin id + private chat id, never trusted from
        callback data

Conventions: every write goes through ``db.transaction()``
(``BEGIN IMMEDIATE``); every read opens its own connection via
``db.get_connection()``.  The transaction helper owns its connection,
so multi-statement operations here are atomic and race-safe (close vs
reply is serialized by the write lock).

Boundaries (this store must NOT):
    - authorize anyone (``config.is_admin`` is the only authorization
      surface — the service layer re-checks it for every action)
    - send Telegram messages or build notification/callback payloads
      (AdminNotifier / support_service own delivery)
    - invent financial behavior (categories are labels only)
    - hold any conversation state in memory
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import db
from task_taxonomy import has_unsafe_control_chars

logger = logging.getLogger(__name__)

# ── Statuses (only the two states the workflow actually needs) ────────
# 'open'   — active: admins may read/reply, the user may append
# 'closed' — terminal: no replies, a later user message opens a NEW
#            inquiry (the partial unique index only counts 'open').
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_OPEN, STATUS_CLOSED)

# ── Categories: stable machine ids + Arabic display labels ────────────
# Labels/routing metadata ONLY — no withdrawal/deposit/wallet behavior
# exists in this task (MT-ADMIN-07/08 own those domains).
CATEGORY_TASK = "task"
CATEGORY_WITHDRAWAL = "withdrawal"
CATEGORY_DEPOSIT = "deposit"
CATEGORY_ACCOUNT = "account"
CATEGORY_OTHER = "other"
CATEGORIES = (
    CATEGORY_TASK,
    CATEGORY_WITHDRAWAL,
    CATEGORY_DEPOSIT,
    CATEGORY_ACCOUNT,
    CATEGORY_OTHER,
)
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_TASK: "مشكلة في مهمة",
    CATEGORY_WITHDRAWAL: "مشكلة في السحب",
    CATEGORY_DEPOSIT: "مشكلة في الإيداع",
    CATEGORY_ACCOUNT: "مشكلة في الحساب",
    CATEGORY_OTHER: "أخرى",
}

# Bounded support text: short enough that a bounded conversation
# window still fits inside one Telegram message (4096 chars).
MAX_MESSAGE_LENGTH = 2000

# ── Row snapshots ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SupportInquiry:
    """Immutable snapshot of one support inquiry — SAFE fields only."""

    id: int
    user_id: int
    category: str
    status: str
    created_at: str
    updated_at: str

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN


@dataclass(frozen=True)
class SupportMessage:
    """Immutable snapshot of one stored support message."""

    id: int
    inquiry_id: int
    sender_type: str
    sender_id: int
    message: str
    source_message_id: int | None
    delivered_at: str | None
    created_at: str

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None


@dataclass(frozen=True)
class SupportReplyContext:
    """Persisted 'admin is answering inquiry X' state (from the DB)."""

    admin_user_id: int
    admin_chat_id: int
    inquiry_id: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserSubmission:
    """Result of one user support-message submission."""

    inquiry: SupportInquiry
    message: SupportMessage
    created: bool  # True → this submission OPENED a new inquiry
    duplicate: bool  # True → replayed Telegram update, nothing new


@dataclass(frozen=True)
class AdminSubmission:
    """Result of one admin reply persisted for an open inquiry."""

    inquiry: SupportInquiry
    message: SupportMessage
    duplicate: bool  # True → replayed Telegram update, nothing new


# ── Errors ────────────────────────────────────────────────────────────


class SupportStoreError(Exception):
    """Base class for support store failures."""


class SupportValidationError(SupportStoreError, ValueError):
    """Untrusted input (text/category/id) failed validation."""


# ── Input validation (untrusted values, validated server-side) ────────


def validate_category(value: object) -> str:
    """Return the canonical category id or raise ``SupportValidationError``."""
    if not isinstance(value, str) or value not in CATEGORIES:
        raise SupportValidationError("unsupported support category")
    return value


def validate_message_text(value: object) -> str:
    """Validate and normalize one bounded support message.

    Rejects: non-strings, empty/whitespace-only text, over-long text
    and unsafe C0/C1 control characters (tab/newline are fine — a
    support message may be multi-line).  Returns the stripped text.
    """
    if not isinstance(value, str):
        raise SupportValidationError("support message must be text")
    text = value.strip()
    if not text:
        raise SupportValidationError("support message must not be empty")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise SupportValidationError(
            f"support message exceeds {MAX_MESSAGE_LENGTH} characters"
        )
    if has_unsafe_control_chars(text, allow_newlines=True):
        raise SupportValidationError(
            "support message contains unsafe control characters"
        )
    return text


def _require_user_id(value: object, name: str) -> int:
    """A positive, non-bool integer Telegram id or ``SupportValidationError``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SupportValidationError(f"{name} must be a positive integer")
    return value


def _normalize_source_id(value: object) -> int | None:
    """Telegram source message id → positive int, or None when absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# NOTE: "unsafe control characters" is validated with the existing
# shared helper from task_taxonomy (tab always allowed, newline only
# when the caller permits it).  task_taxonomy imports nothing from the
# project, so this cannot create an import cycle.


# ── Row helpers ───────────────────────────────────────────────────────

_INQUIRY_COLUMNS = "id, user_id, category, status, created_at, updated_at"
_MESSAGE_COLUMNS = (
    "id, inquiry_id, sender_type, sender_id, message, "
    "source_message_id, delivered_at, created_at"
)


def _row_to_inquiry(row) -> SupportInquiry:
    return SupportInquiry(
        id=row["id"],
        user_id=row["user_id"],
        category=row["category"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row) -> SupportMessage:
    return SupportMessage(
        id=row["id"],
        inquiry_id=row["inquiry_id"],
        sender_type=row["sender_type"],
        sender_id=row["sender_id"],
        message=row["message"],
        source_message_id=row["source_message_id"],
        delivered_at=row["delivered_at"],
        created_at=row["created_at"],
    )


def _row_to_context(row) -> SupportReplyContext:
    return SupportReplyContext(
        admin_user_id=row["admin_user_id"],
        admin_chat_id=row["admin_chat_id"],
        inquiry_id=row["inquiry_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── Reads ─────────────────────────────────────────────────────────────


def get_inquiry(inquiry_id: object, db_path: str | None = None) -> SupportInquiry | None:
    """Fetch one inquiry by id; None for missing/invalid ids."""
    if isinstance(inquiry_id, bool) or not isinstance(inquiry_id, int):
        return None
    if inquiry_id <= 0:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
    return _row_to_inquiry(row) if row else None


def get_active_inquiry(
    user_id: object, db_path: str | None = None
) -> SupportInquiry | None:
    """The user's ONE open inquiry (DB-enforced unique), or None."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
            "WHERE user_id = ? AND status = ? ORDER BY id ASC LIMIT 1",
            (user_id, STATUS_OPEN),
        ).fetchone()
    return _row_to_inquiry(row) if row else None


def list_open_inquiries(db_path: str | None = None) -> list[SupportInquiry]:
    """Every non-closed inquiry, oldest first (deterministic ``id ASC``).

    Read-only: listing never mutates any row.
    """
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
            "WHERE status = ? ORDER BY id ASC",
            (STATUS_OPEN,),
        ).fetchall()
    return [_row_to_inquiry(r) for r in rows]


def list_messages(
    inquiry_id: object, limit: int = 5, db_path: str | None = None
) -> list[SupportMessage]:
    """The newest *limit* messages of one inquiry, oldest-first within
    the window (deterministic ordering by ``id ASC``)."""
    if isinstance(inquiry_id, bool) or not isinstance(inquiry_id, int) or inquiry_id <= 0:
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM support_messages "
            "WHERE inquiry_id = ? ORDER BY id DESC LIMIT ?",
            (inquiry_id, limit),
        ).fetchall()
    return [_row_to_message(r) for r in reversed(rows)]


def get_pending_category(
    user_id: object, db_path: str | None = None
) -> str | None:
    """The persisted category the user selected, or None."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT category FROM support_user_states WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row["category"] if row else None


def get_reply_context(
    admin_user_id: object,
    admin_chat_id: object,
    db_path: str | None = None,
) -> SupportReplyContext | None:
    """Resolve the persisted reply context from admin id + chat id.

    Both values must match the stored row — the context is server
    state, never something a callback or client message can forge.
    """
    if isinstance(admin_user_id, bool) or not isinstance(admin_user_id, int):
        return None
    if isinstance(admin_chat_id, bool) or not isinstance(admin_chat_id, int):
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT admin_user_id, admin_chat_id, inquiry_id, "
            "created_at, updated_at FROM support_reply_contexts "
            "WHERE admin_user_id = ? AND admin_chat_id = ?",
            (admin_user_id, admin_chat_id),
        ).fetchone()
    return _row_to_context(row) if row else None


# ── User-side flow state ──────────────────────────────────────────────


def set_pending_category(
    user_id: object, category: object, db_path: str | None = None
) -> str:
    """Persist the user's category selection (upsert).  Validated."""
    uid = _require_user_id(user_id, "user_id")
    cat = validate_category(category)
    with db.transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO support_user_states (user_id, category, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "category = excluded.category, "
            "updated_at = CURRENT_TIMESTAMP",
            (uid, cat),
        )
    return cat


def clear_pending_category(user_id: object, db_path: str | None = None) -> bool:
    """Drop any in-progress category selection.  Idempotent."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    with db.transaction(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM support_user_states WHERE user_id = ?", (user_id,)
        )
    return cursor.rowcount > 0


# ── User message submission (create-or-append, replay-safe) ───────────


def _find_by_source(
    conn: sqlite3.Connection,
    sender_type: str,
    sender_id: int,
    source_message_id: int,
):
    return conn.execute(
        f"SELECT {_MESSAGE_COLUMNS} FROM support_messages "
        "WHERE sender_type = ? AND sender_id = ? AND source_message_id = ?",
        (sender_type, sender_id, source_message_id),
    ).fetchone()


def submit_user_message(
    user_id: object,
    category: object,
    text: object,
    source_message_id: object = None,
    db_path: str | None = None,
) -> UserSubmission:
    """Persist one user support message — atomically.

    Policy (one active inquiry per user):
    - the user has an open inquiry → the message APPENDS to it
      (no duplicate parallel threads are ever created);
    - no open inquiry (first message, or previous one closed) → a new
      inquiry is created with the given category;
    - a replayed Telegram update (same sender + source message id)
      appends NOTHING and returns the original rows.

    All of it runs inside ONE ``db.transaction()`` so a duplicate can
    never leave a half-created inquiry behind.
    """
    uid = _require_user_id(user_id, "user_id")
    cat = validate_category(category)
    msg_text = validate_message_text(text)
    source_id = _normalize_source_id(source_message_id)

    with db.transaction(db_path) as conn:
        # 1. Replay guard — a redelivered update must be a no-op.
        if source_id is not None:
            dup = _find_by_source(conn, "user", uid, source_id)
            if dup is not None:
                inquiry = _row_to_inquiry(
                    conn.execute(
                        f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
                        "WHERE id = ?",
                        (dup["inquiry_id"],),
                    ).fetchone()
                )
                conn.execute(
                    "DELETE FROM support_user_states WHERE user_id = ?", (uid,)
                )
                return UserSubmission(
                    inquiry=inquiry,
                    message=_row_to_message(dup),
                    created=False,
                    duplicate=True,
                )

        # 2. Create-or-append (DB unique index arbitrates the race).
        active = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
            "WHERE user_id = ? AND status = ? ORDER BY id ASC LIMIT 1",
            (uid, STATUS_OPEN),
        ).fetchone()
        if active is not None:
            inquiry_id = active["id"]
            created = False
        else:
            try:
                cursor = conn.execute(
                    "INSERT INTO support_inquiries "
                    "(user_id, category, status) VALUES (?, ?, ?)",
                    (uid, cat, STATUS_OPEN),
                )
            except sqlite3.IntegrityError:
                # Lost the one-active-inquiry race → append to winner.
                active = conn.execute(
                    f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
                    "WHERE user_id = ? AND status = ? ORDER BY id ASC LIMIT 1",
                    (uid, STATUS_OPEN),
                ).fetchone()
                if active is None:
                    raise
                inquiry_id = active["id"]
                created = False
            else:
                inquiry_id = cursor.lastrowid
                created = True

        # 3. Persist the message (delivered: its fate is the admin
        #    notification, which the service sends fail-soft next).
        try:
            cursor = conn.execute(
                "INSERT INTO support_messages "
                "(inquiry_id, sender_type, sender_id, message, "
                " source_message_id, delivered_at) "
                "VALUES (?, 'user', ?, ?, ?, CURRENT_TIMESTAMP)",
                (inquiry_id, uid, msg_text, source_id),
            )
        except sqlite3.IntegrityError:
            # Duplicate (sender_type, sender_id, source_message_id):
            # a replay slipped past step 1 — roll the whole thing back.
            if source_id is None:
                raise
            dup = _find_by_source(conn, "user", uid, source_id)
            if dup is None:
                raise
            inquiry = _row_to_inquiry(
                conn.execute(
                    f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries "
                    "WHERE id = ?",
                    (dup["inquiry_id"],),
                ).fetchone()
            )
            conn.execute(
                "DELETE FROM support_user_states WHERE user_id = ?", (uid,)
            )
            return UserSubmission(
                inquiry=inquiry,
                message=_row_to_message(dup),
                created=False,
                duplicate=True,
            )
        message_id = cursor.lastrowid

        # 4. Bump activity + consume the pending category selection.
        conn.execute(
            "UPDATE support_inquiries SET updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (inquiry_id,),
        )
        conn.execute(
            "DELETE FROM support_user_states WHERE user_id = ?", (uid,)
        )

        row = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
        message_row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM support_messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    logger.info(
        "Support user message stored: user=%d inquiry=%d created=%s",
        uid, inquiry_id, created,
    )
    return UserSubmission(
        inquiry=_row_to_inquiry(row),
        message=_row_to_message(message_row),
        created=created,
        duplicate=False,
    )


# ── Admin reply (persist-first, delivery handled by the service) ──────


def append_admin_message(
    inquiry_id: object,
    admin_user_id: object,
    text: object,
    source_message_id: object = None,
    db_path: str | None = None,
) -> AdminSubmission | None:
    """Persist one admin reply against an OPEN inquiry.

    Returns None when the inquiry is missing or closed (a stale reply
    never mutates anything).  The message row is written BEFORE any
    Telegram delivery attempt — delivery failure can therefore never
    lose the admin's text, and a replayed update (same source message
    id) appends nothing twice.
    """
    iid = _require_user_id(inquiry_id, "inquiry_id")
    aid = _require_user_id(admin_user_id, "admin_user_id")
    msg_text = validate_message_text(text)
    source_id = _normalize_source_id(source_message_id)

    with db.transaction(db_path) as conn:
        inquiry_row = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries WHERE id = ?",
            (iid,),
        ).fetchone()
        if inquiry_row is None:
            return None

        if source_id is not None:
            dup = _find_by_source(conn, "admin", aid, source_id)
            if dup is not None:
                return AdminSubmission(
                    inquiry=_row_to_inquiry(inquiry_row),
                    message=_row_to_message(dup),
                    duplicate=True,
                )

        # Close-vs-reply race: BEGIN IMMEDIATE already serialized us
        # against close_inquiry(); re-read status inside the lock.
        if inquiry_row["status"] != STATUS_OPEN:
            return None

        try:
            cursor = conn.execute(
                "INSERT INTO support_messages "
                "(inquiry_id, sender_type, sender_id, message, "
                " source_message_id) "
                "VALUES (?, 'admin', ?, ?, ?)",
                (iid, aid, msg_text, source_id),
            )
        except sqlite3.IntegrityError:
            if source_id is None:
                raise
            dup = _find_by_source(conn, "admin", aid, source_id)
            if dup is None:
                raise
            return AdminSubmission(
                inquiry=_row_to_inquiry(inquiry_row),
                message=_row_to_message(dup),
                duplicate=True,
            )
        message_id = cursor.lastrowid
        conn.execute(
            "UPDATE support_inquiries SET updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (iid,),
        )
        fresh = conn.execute(
            f"SELECT {_INQUIRY_COLUMNS} FROM support_inquiries WHERE id = ?",
            (iid,),
        ).fetchone()
        message_row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM support_messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    logger.info(
        "Support admin reply stored: inquiry=%d admin=%d message=%d",
        iid, aid, message_id,
    )
    return AdminSubmission(
        inquiry=_row_to_inquiry(fresh),
        message=_row_to_message(message_row),
        duplicate=False,
    )


def mark_message_delivered(
    message_id: object, db_path: str | None = None
) -> bool:
    """Stamp an admin message as delivered (idempotent)."""
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return False
    with db.transaction(db_path) as conn:
        cursor = conn.execute(
            "UPDATE support_messages SET delivered_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND delivered_at IS NULL",
            (message_id,),
        )
    return cursor.rowcount > 0


def get_message(message_id: object, db_path: str | None = None) -> SupportMessage | None:
    """Fetch one stored support message by id."""
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM support_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return _row_to_message(row) if row else None


# ── Admin reply context (persisted draft) ─────────────────────────────


def set_reply_context(
    admin_user_id: object,
    admin_chat_id: object,
    inquiry_id: object,
    db_path: str | None = None,
) -> SupportReplyContext | None:
    """Persist 'admin X (chat Y) is answering inquiry Z' (upsert).

    Verifies inside the same transaction that the inquiry still exists
    and is OPEN — a stale reply button on a closed inquiry stores
    nothing.  Returns the stored context, or None when rejected.
    """
    aid = _require_user_id(admin_user_id, "admin_user_id")
    cid = _require_user_id(admin_chat_id, "admin_chat_id")
    iid = _require_user_id(inquiry_id, "inquiry_id")
    with db.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT id, status FROM support_inquiries WHERE id = ?",
            (iid,),
        ).fetchone()
        if row is None or row["status"] != STATUS_OPEN:
            return None
        conn.execute(
            "INSERT INTO support_reply_contexts "
            "(admin_user_id, admin_chat_id, inquiry_id, created_at, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT(admin_user_id) DO UPDATE SET "
            "admin_chat_id = excluded.admin_chat_id, "
            "inquiry_id = excluded.inquiry_id, "
            "updated_at = CURRENT_TIMESTAMP",
            (aid, cid, iid),
        )
        stored = conn.execute(
            "SELECT admin_user_id, admin_chat_id, inquiry_id, "
            "created_at, updated_at FROM support_reply_contexts "
            "WHERE admin_user_id = ?",
            (aid,),
        ).fetchone()
    return _row_to_context(stored)


def clear_reply_context(
    admin_user_id: object,
    inquiry_id: object = None,
    db_path: str | None = None,
) -> bool:
    """Delete an admin's reply context (optionally only if it points
    at *inquiry_id*).  Idempotent."""
    if isinstance(admin_user_id, bool) or not isinstance(admin_user_id, int) or admin_user_id <= 0:
        return False
    with db.transaction(db_path) as conn:
        if inquiry_id is None:
            cursor = conn.execute(
                "DELETE FROM support_reply_contexts WHERE admin_user_id = ?",
                (admin_user_id,),
            )
        else:
            if (
                isinstance(inquiry_id, bool)
                or not isinstance(inquiry_id, int)
                or inquiry_id <= 0
            ):
                return False
            cursor = conn.execute(
                "DELETE FROM support_reply_contexts "
                "WHERE admin_user_id = ? AND inquiry_id = ?",
                (admin_user_id, inquiry_id),
            )
    return cursor.rowcount > 0


# ── Close (server-side, idempotent) ───────────────────────────────────


def close_inquiry(
    inquiry_id: object, db_path: str | None = None
) -> bool | None:
    """Close one inquiry inside a single transaction.

    Returns:
        True  — the inquiry was open and is now closed
        False — already closed (idempotent no-op)
        None  — inquiry does not exist

    Closing also deletes every reply context pointing at the inquiry,
    so a stale reply context can never send into a closed thread.
    """
    if isinstance(inquiry_id, bool) or not isinstance(inquiry_id, int) or inquiry_id <= 0:
        return None
    with db.transaction(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM support_inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == STATUS_CLOSED:
            return False
        conn.execute(
            "UPDATE support_inquiries SET status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (STATUS_CLOSED, inquiry_id),
        )
        conn.execute(
            "DELETE FROM support_reply_contexts WHERE inquiry_id = ?",
            (inquiry_id,),
        )
    logger.info("Support inquiry closed: inquiry=%d", inquiry_id)
    return True
