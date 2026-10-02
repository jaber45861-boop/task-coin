"""
SQLite persistence for required channels and user/referral tracking.

This module provides a thin layer over SQLite to store and load
the required-channel configuration and user referral data.
"""

import sqlite3
import os
import logging
import threading
from typing import Iterator, Optional
from contextlib import contextmanager

from config import ADMINS, Channel, CHANNELS
# USDT scale authority (1 USDT = 100,000,000 atomic units).  Importing
# the module (not its attributes) keeps the runtime-only cycle safe in
# both import orders: neither module touches the other at import time.
import wallet

logger = logging.getLogger(__name__)

# ── Allowed user_task statuses ─────────────────────────────────────
USER_TASK_STATUS_AVAILABLE = "available"
USER_TASK_STATUS_STARTED = "started"
USER_TASK_STATUS_COMPLETED = "completed"

# Task repeat policy (MT-TASK-04).  "one_time" tasks are terminal for a
# user; "repeatable" tasks may start a new cycle after repeat_hours.
REPEAT_POLICY_ONE_TIME = "one_time"
REPEAT_POLICY_REPEATABLE = "repeatable"
REPEAT_POLICIES = (REPEAT_POLICY_ONE_TIME, REPEAT_POLICY_REPEATABLE)

# Submission-level states (MT-TASK-04).  These describe a verification
# attempt and are deliberately separate from the user_tasks lifecycle.
SUBMISSION_STATUS_SUBMITTED = "submitted"
SUBMISSION_STATUS_PASSED = "passed"
SUBMISSION_STATUS_FAILED = "failed"
SUBMISSION_STATUS_ERROR = "error"
SUBMISSION_STATUSES = (
    SUBMISSION_STATUS_SUBMITTED,
    SUBMISSION_STATUS_PASSED,
    SUBMISSION_STATUS_FAILED,
    SUBMISSION_STATUS_ERROR,
)

# Buyer-approval states for approval-gated submissions (MT-TASK-06).
# Lives on task_submissions (submission layer) — NEVER on user_tasks.
# NULL in approval_status means "not approval-gated" (every non-referral
# submission, and all legacy rows).
SUBMISSION_APPROVAL_PENDING = "pending"
SUBMISSION_APPROVAL_APPROVED = "approved"
SUBMISSION_APPROVAL_REJECTED = "rejected"
SUBMISSION_APPROVAL_STATES = (
    SUBMISSION_APPROVAL_PENDING,
    SUBMISSION_APPROVAL_APPROVED,
    SUBMISSION_APPROVAL_REJECTED,
)
ALLOWED_USER_TASK_STATUSES = {
    USER_TASK_STATUS_AVAILABLE,
    USER_TASK_STATUS_STARTED,
    USER_TASK_STATUS_COMPLETED,
}

DB_PATH = os.environ.get("TASKCOIN_DB_PATH", "task_coin.db")

# SQLite INTEGER is a signed 64-bit value.  The reward backfill range-
# checks against these bounds so an absurd reward can never be stored
# as a wrapped or REAL (float) value.
_SQLITE_INT64_MIN = -9_223_372_036_854_775_808
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

# How long a connection waits for a lock before SQLite raises SQLITE_BUSY.
# Applied to every connection at the connection layer (no retry loops).
BUSY_TIMEOUT_MS = 5000


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the repository's standard connection settings.

    Centralized so every connection — regular helper scopes and the
    ``transaction()`` primitive alike — gets identical guarantees:
    row factory, WAL journal mode, foreign keys, busy timeout.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


@contextmanager
def get_connection(db_path: str | None = None):
    """Get a SQLite connection with WAL mode for better concurrency."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class NestedTransactionError(RuntimeError):
    """Raised when ``transaction()`` is entered while already active.

    Nested (savepoint-style) transactions are deliberately not supported;
    the repository rejects them explicitly instead of pretending that a
    nested BEGIN is valid.
    """


# Per-thread "am I already inside transaction() on this thread?" flag so a
# same-thread nested entry is rejected explicitly instead of deadlocking
# against the helper's own write lock.  Threads stay fully independent.
_transaction_state = threading.local()


@contextmanager
def transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Run a block of work inside an atomic ``BEGIN IMMEDIATE`` transaction.

    Generic infrastructure only: it knows nothing about wallets, ledgers,
    withdrawals, tasks or Telegram.  Future services pass the yielded
    connection to any component that accepts a caller-owned connection
    (e.g. ``LedgerService(connection=conn)``).

    Semantics:

    - this helper *owns* the connection it opens: it configures it exactly
      like ``get_connection()``, begins the transaction, commits on success,
      rolls back on any exception, and closes it exactly once — it never
      touches caller-owned connections
    - the transaction starts with ``BEGIN IMMEDIATE`` so the write lock is
      established before any read/modify/write sequence
    - exceptions are never swallowed: the original exception is re-raised
      after rollback, and no application operation is ever retried
    - re-entering ``transaction()`` on a thread where it is already active
      raises :class:`NestedTransactionError` (no implicit nested BEGINs,
      no savepoints)

    Args:
        db_path: database file; defaults to ``DB_PATH``.

    Yields:
        The open connection, already inside the transaction.  Values
        assigned inside the ``with`` block become the caller's result by
        ordinary Python assignment.

    Raises:
        NestedTransactionError: on same-thread nested entry.
    """
    if getattr(_transaction_state, "active", False):
        raise NestedTransactionError(
            "transaction() is already active on this thread; "
            "nested transactions are not supported"
        )
    if db_path is None:
        db_path = DB_PATH
    # isolation_level=None switches off sqlite3's implicit BEGIN/COMMIT so
    # this helper — and only this helper — controls transaction boundaries.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        _configure_connection(conn)
        _transaction_state.active = True
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        _transaction_state.active = False
        conn.close()


# ── Supported user languages ────────────────────────────────────────────
# Persisted per user in users.language.  The value is constrained to
# exactly these codes by is_supported_language() / set_user_language().
SUPPORTED_LANGUAGES: tuple[str, ...] = ("ar", "en", "ru", "fa")


# ── Administrative administrators (MT-ADMIN-37) ──────────────────────
# THE authoritative persistent admin registry: the single source the
# Admin Control Center's ``admins`` module manages (list / detail /
# add / remove) and ONE half of the single centralized authorization
# decision — ``config.is_admin`` consults ``is_admin_user`` after the
# configured bootstrap list (``config.ADMINS``), never a second one.
# Identity is ALWAYS the Telegram numeric user_id; usernames, user
# passwords, tokens, initData and profile dumps are never stored.


def _validate_admin_user_id(user_id: object) -> int:
    """Reject anything that is not a positive, int64-safe id."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or user_id > _SQLITE_INT64_MAX
    ):
        raise ValueError("admin user_id must be a positive int")
    return user_id


def is_admin_user(user_id: int, db_path: str | None = None) -> bool:
    """True when *user_id* is an ACTIVE administrator (the store
    half of the single ``config.is_admin`` decision).

    Defensive by design: authorization fails CLOSED and never raises
    — a missing database file or a missing table (a fresh path, not
    yet initialized) answers False WITHOUT creating anything.
    """
    if db_path is None:
        db_path = DB_PATH
    if not os.path.exists(db_path):
        return False  # never create a stray file for a lookup
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM admin_users WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False  # uninitialized schema — fail closed


def list_admin_users(
    active_only: bool = False, db_path: str | None = None
) -> list[dict]:
    """Administrator rows in deterministic ``user_id ASC`` order.

    The ONE authoritative admin list read (read-only).  The registry
    is small by nature; the Control Center still pages it.
    """
    with get_connection(db_path) as conn:
        query = (
            "SELECT user_id, active, created_at, added_by FROM admin_users"
        )
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY user_id ASC"
        return [dict(row) for row in conn.execute(query).fetchall()]


def get_admin_user(
    user_id: int, db_path: str | None = None
) -> dict | None:
    """One administrator row (active flag included), or None."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, active, created_at, added_by "
            "FROM admin_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def add_admin_user(
    user_id: int, added_by: int | None = None,
    db_path: str | None = None,
) -> str:
    """Ensure *user_id* is an active administrator — ONE idempotent
    authoritative operation (the add-confirm calls it exactly once).

    Returns:
        ``created``      a new row was inserted
        ``reactivated``  an existing inactive row was re-enabled
        ``exists``       already active — NO write (duplicate-proof)

    Concurrency: the insert path is IntegrityError-safe, so two
    simultaneous confirmations can never create duplicate records.
    """
    _validate_admin_user_id(user_id)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT active FROM admin_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            try:
                conn.execute(
                    "INSERT INTO admin_users (user_id, active, added_by) "
                    "VALUES (?, 1, ?)",
                    (user_id, added_by),
                )
                return "created"
            except sqlite3.IntegrityError:
                return "exists"  # lost a benign race — idempotent
        if row["active"]:
            return "exists"
        conn.execute(
            "UPDATE admin_users SET active = 1 WHERE user_id = ?",
            (user_id,),
        )
        return "reactivated"


def remove_admin_user(
    user_id: int, db_path: str | None = None
) -> str:
    """Soft-remove: flip active 1 → 0 — ONE authoritative operation.

    Returns:
        ``removed``         was active, now off
        ``already_removed`` inactive row (idempotent, no write)
        ``missing``         no such row

    Policy checks (configured-bootstrap / final-admin) belong to the
    authorized caller — this primitive only records the state change.
    """
    _validate_admin_user_id(user_id)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE admin_users SET active = 0 "
            "WHERE user_id = ? AND active = 1",
            (user_id,),
        )
        if cursor.rowcount:
            return "removed"
        row = conn.execute(
            "SELECT 1 FROM admin_users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return "already_removed" if row else "missing"


# ── Admin broadcast (MT-ADMIN-38) ─────────────────────────────────
# Persistence for the Control Center's ``broadcast`` module.  ALL
# broadcast state lives in SQLite — never in a process-global dict —
# so drafts survive handler recreation and restarts, and the atomic
# ``draft → sending`` UPDATE is the ONLY duplicate-send authority:
# double-clicks, delayed/stale callbacks and cross-client replays
# all lose the same single transition and can never start a second
# delivery pass.  This section is a pure record store: no wallet,
# ledger, task, reward, rate, payment-method or admin-role column is
# ever read or written here, and no recipient list is ever produced
# from anywhere but the authoritative ``users`` table
# (``list_broadcast_recipient_ids``).
#
# Status lifecycle (explicit CHECK constraint on the table):
#   draft      armed/compose state; message may still be '' — the
#              ONLY confirmable state
#   sending    claimed by exactly ONE confirm (crash here leaves the
#              row honestly 'sending': never auto-resumed, never
#              re-sent, stale confirms answer already-processed)
#   completed  the delivery pass finished (counts stamped)
#   cancelled  cancelled before any send (no send ever happened)

BROADCAST_STATUS_DRAFT = "draft"
BROADCAST_STATUS_SENDING = "sending"
BROADCAST_STATUS_COMPLETED = "completed"
BROADCAST_STATUS_CANCELLED = "cancelled"

# Telegram's hard limit for a plain text message (characters).  The
# store enforces it again on every write — the UI validation is not
# the only gate.
MAX_BROADCAST_MESSAGE_LEN = 4096

# Hard bound on recipient enumeration: the id-only read below can
# never pull an unbounded table into memory, and a population above
# the bound FAILS CLOSED instead of starting a silently partial
# broadcast.
MAX_BROADCAST_RECIPIENTS = 50_000


def _validate_broadcast_actor(user_id: object) -> int:
    """Reject anything that is not a positive, int64-safe id."""
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or user_id > _SQLITE_INT64_MAX
    ):
        raise ValueError("broadcast admin_user_id must be a positive int")
    return user_id


def arm_broadcast_draft(
    admin_user_id: int, db_path: str | None = None
) -> int:
    """Create — or reset — the ONE open draft for this administrator.

    This is the persisted compose state behind ``ctl:broadcast:new``:
    a fresh row starts with an empty message ("awaiting text"), and
    pressing ``new`` again resets any existing draft back to that
    composing state.  At most ONE draft per admin can be open
    (partial unique index), so the confirm/cancel payloads never
    need an identifier — and never the message body.

    Returns the draft id.  Race-safe: a lost INSERT race reuses the
    winner's row instead of failing.
    """
    _validate_broadcast_actor(admin_user_id)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM broadcasts "
            "WHERE admin_user_id = ? AND status = 'draft'",
            (admin_user_id,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE broadcasts SET message = '' WHERE id = ?",
                (row["id"],),
            )
            return row["id"]
        try:
            cursor = conn.execute(
                "INSERT INTO broadcasts (admin_user_id, message) "
                "VALUES (?, '')",
                (admin_user_id,),
            )
        except sqlite3.IntegrityError:
            # Lost a benign race against another arm — reuse it.
            row = conn.execute(
                "SELECT id FROM broadcasts "
                "WHERE admin_user_id = ? AND status = 'draft'",
                (admin_user_id,),
            ).fetchone()
            if row is None:
                raise
            conn.execute(
                "UPDATE broadcasts SET message = '' WHERE id = ?",
                (row["id"],),
            )
            return row["id"]
        return int(cursor.lastrowid)


def get_open_broadcast(
    admin_user_id: int, db_path: str | None = None
) -> dict | None:
    """The admin's open (``draft``) broadcast row, or None.

    Read-only.  The message field is '' while composing — callers
    distinguish compose state (empty) from a confirmable draft
    (non-empty).  At most one row can match (partial unique index).
    """
    _validate_broadcast_actor(admin_user_id)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, admin_user_id, message, status, recipient_count, "
            "success_count, failure_count, created_at "
            "FROM broadcasts "
            "WHERE admin_user_id = ? AND status = 'draft' "
            "ORDER BY id DESC LIMIT 1",
            (admin_user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_latest_broadcast(
    admin_user_id: int, db_path: str | None = None
) -> dict | None:
    """The admin's most recent broadcast row (any status), or None.

    Read-only — used to answer a STALE confirm deterministically:
    a sending/completed row means ``already processed``, anything
    else means ``no pending operation``.  Never resends.
    """
    _validate_broadcast_actor(admin_user_id)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, admin_user_id, message, status, recipient_count, "
            "success_count, failure_count, created_at "
            "FROM broadcasts "
            "WHERE admin_user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (admin_user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_broadcast_draft_message(
    broadcast_id: int, message: str, db_path: str | None = None
) -> bool:
    """Store the composed text on the open draft — single-use state.

    Returns True only while the row is still ``draft`` (a cancelled,
    claimed or vanished draft writes NOTHING and answers False, so a
    raced text can never resurrect a processed broadcast).

    Validation is re-enforced here: non-empty after strip, bounded by
    Telegram's real text limit — the UI check is not the only gate.
    """
    if (
        isinstance(broadcast_id, bool)
        or not isinstance(broadcast_id, int)
        or broadcast_id <= 0
    ):
        raise ValueError("broadcast_id must be a positive int")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("broadcast message must be non-empty text")
    if len(message) > MAX_BROADCAST_MESSAGE_LEN:
        raise ValueError("broadcast message exceeds Telegram's text limit")
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE broadcasts SET message = ? "
            "WHERE id = ? AND status = 'draft'",
            (message, broadcast_id),
        )
        return cursor.rowcount == 1


def claim_broadcast_sending(
    broadcast_id: int, recipient_count: int, db_path: str | None = None
) -> bool:
    """THE atomic duplicate-send gate: ``draft → sending``, once.

    Exactly ONE caller can observe rowcount == 1 (a single guarded
    UPDATE — no application-level lock involved); every other answer
    (already sending/completed/cancelled/missing) means NO Telegram
    send may begin.  ``recipient_count`` is stamped with the claim so
    the final invariant (success + failure == recipients) is recorded
    against the population the winning pass actually enumerated.
    """
    if (
        isinstance(broadcast_id, bool)
        or not isinstance(broadcast_id, int)
        or broadcast_id <= 0
    ):
        raise ValueError("broadcast_id must be a positive int")
    if (
        isinstance(recipient_count, bool)
        or not isinstance(recipient_count, int)
        or recipient_count < 0
    ):
        raise ValueError("recipient_count must be a non-negative int")
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE broadcasts "
            "SET status = 'sending', recipient_count = ?, "
            "confirmed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'draft'",
            (recipient_count, broadcast_id),
        )
        return cursor.rowcount == 1


def finalize_broadcast(
    broadcast_id: int,
    success_count: int,
    failure_count: int,
    db_path: str | None = None,
) -> bool:
    """Stamp the finished pass: ``sending → completed`` with counts.

    Guarded on ``status = 'sending'`` — it never resurrects a
    cancelled draft and never re-opens a completed row.  Returns True
    only for the row this caller actually owned.
    """
    if (
        isinstance(broadcast_id, bool)
        or not isinstance(broadcast_id, int)
        or broadcast_id <= 0
    ):
        raise ValueError("broadcast_id must be a positive int")
    for value in (success_count, failure_count):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError("counts must be non-negative ints")
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE broadcasts "
            "SET status = 'completed', success_count = ?, "
            "failure_count = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'sending'",
            (success_count, failure_count, broadcast_id),
        )
        return cursor.rowcount == 1


def cancel_open_broadcast(
    admin_user_id: int, db_path: str | None = None
) -> bool:
    """Cancel the admin's open draft — ``draft → cancelled``.

    Only drafts are affected: a sending or completed broadcast is
    never mutated by a cancel, and cancelling performs no send and
    no user mutation.  Returns False when there was nothing to
    cancel (idempotent — a repeated cancel is a safe no-op).
    """
    _validate_broadcast_actor(admin_user_id)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "UPDATE broadcasts SET status = 'cancelled' "
            "WHERE admin_user_id = ? AND status = 'draft'",
            (admin_user_id,),
        )
        return cursor.rowcount >= 1


def init_db(db_path: str | None = None) -> None:
    """Initialize all database tables (channels + users)."""
    with get_connection(db_path) as conn:
        # ── Required channels table ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS required_channels (
                slug TEXT PRIMARY KEY,
                channel_id INTEGER NOT NULL UNIQUE,
                username TEXT NOT NULL,
                title TEXT NOT NULL,
                required INTEGER NOT NULL DEFAULT 1,
                chat_type TEXT NOT NULL DEFAULT 'channel'
            )
        """)
        # Migration: add chat_type column if missing (pre-existing DBs)
        try:
            conn.execute("ALTER TABLE required_channels ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'channel'")
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── Users / referral attribution table ──────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referred_by INTEGER,
                language TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referred_by) REFERENCES users(user_id)
            )
        """)
        # Migration: add language column for pre-existing DBs
        try:
            conn.execute("ALTER TABLE users ADD COLUMN language TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── Task definitions table ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                type TEXT NOT NULL,
                reward INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                task_data TEXT,
                repeat_policy TEXT NOT NULL DEFAULT 'one_time',
                repeat_hours INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reward_units INTEGER,
                commission_units INTEGER,
                CHECK (repeat_policy IN ('one_time', 'repeatable')),
                CHECK (
                    (repeat_policy = 'one_time' AND repeat_hours IS NULL)
                    OR (repeat_policy = 'repeatable'
                        AND repeat_hours IS NOT NULL AND repeat_hours >= 1)
                )
            )
        """)
        # Migration: add task_data column if missing (pre-existing DBs)
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_data TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: repeat-policy columns for pre-existing DBs (MT-TASK-04).
        # Existing rows receive the safe defaults: one_time + NULL hours.
        # Cross-column validation for migrated tables is enforced by the
        # application layer (db.create_task / db.update_task).
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN repeat_policy TEXT "
                "NOT NULL DEFAULT 'one_time'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN repeat_hours INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── Authoritative atomic task reward (MT-ADMIN-13) ─────────
        # Additive migration: ``reward_units`` stores the reward in
        # exact USDT atomic units (1 USDT = 100,000,000) and is the
        # value settlement reads.  ``tasks.reward`` keeps its existing
        # whole-USDT meaning untouched.
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN reward_units INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Backfill every not-yet-populated row exactly once:
        # reward_units = reward * USDT_SCALE in Python integer
        # arithmetic.  SQLite is never asked to multiply: its integer
        # arithmetic silently promotes to REAL on overflow, which
        # would store float money.  Only rows still NULL are touched,
        # so re-running init_db() can never re-multiply a populated
        # value.  A non-integer or out-of-int64 reward stays NULL —
        # settlement's validation then rejects such a row exactly as
        # it did before this migration (no corruption, no guesswork).
        for row in conn.execute(
            "SELECT id, reward FROM tasks WHERE reward_units IS NULL"
        ).fetchall():
            reward = row["reward"]
            if not isinstance(reward, int) or isinstance(reward, bool):
                continue  # corrupt/legacy junk: left NULL on purpose
            units = reward * wallet.USDT_SCALE
            if units < _SQLITE_INT64_MIN or units > _SQLITE_INT64_MAX:
                logger.warning(
                    "task %s: reward %s overflows INTEGER atomic units; "
                    "reward_units left NULL",
                    row["id"], reward,
                )
                continue
            conn.execute(
                "UPDATE tasks SET reward_units = ? WHERE id = ?",
                (units, row["id"]),
            )

        # ── Advertiser commission snapshot (MT-ADMIN-16) ──────────
        # Additive migration: ``commission_units`` is the EXACT atomic
        # advertiser commission resolved from the platform setting
        # ``advertiser_commission`` at task CREATION time — an integer
        # snapshot written once and never recomputed (an admin setting
        # change never alters an already-created task).  Rows created
        # before this column stay NULL: their commission was never
        # resolved, and inventing one here would rewrite history.
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN commission_units INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── User task state table ────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                PRIMARY KEY (user_id, task_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        """)

        # ── Task funding record (roadmap 4: advertiser funding) ────
        # One row per funded task — the immutable per-task funding
        # amounts actually charged from the advertiser's wallet inside
        # the creation transaction.  ``task_id`` is the PRIMARY KEY, so
        # the database itself makes "charged twice for one task"
        # impossible: the second insert fails and the whole funding
        # transaction rolls back.  The CHECK constraint pins
        # ``total_units = reward_units + commission_units`` (the
        # advertiser's total cost is worker reward + the immutable
        # commission snapshot — never recomputed from the live
        # setting).  Additive CREATE IF NOT EXISTS: pre-existing
        # databases gain the table on the next init_db(), existing
        # unfunded tasks simply have no row (funding was deferred for
        # them at creation time — nothing is invented retroactively).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_funding (
                task_id INTEGER PRIMARY KEY,
                advertiser_id INTEGER NOT NULL,
                reward_units INTEGER NOT NULL,
                commission_units INTEGER NOT NULL,
                total_units INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (reward_units >= 0),
                CHECK (commission_units >= 0),
                CHECK (total_units >= 0),
                CHECK (total_units = reward_units + commission_units),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (advertiser_id) REFERENCES users(user_id)
            )
        """)

        # ── Task submissions / attempts table (MT-TASK-04) ──────────
        # Auditable history of verification attempts.  Deliberately
        # separate from user_tasks (which keeps only the current cycle):
        #   status: submitted → passed | failed | error  (terminal)
        #   (user_id, task_id) is the user_tasks identity; together with
        #   idempotency_key it forms the database-enforced uniqueness
        #   unit so a repeated key can never create a second record.
        # Timestamps follow the repository convention (TIMESTAMP /
        # CURRENT_TIMESTAMP, UTC); no REAL columns anywhere.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK (status IN
                        ('submitted', 'passed', 'failed', 'error')),
                idempotency_key TEXT NOT NULL,
                verification_reason TEXT,
                submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                UNIQUE (user_id, task_id, idempotency_key)
            )
        """)
        # History lookups per user/task, oldest first.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_submissions_user_task
            ON task_submissions (user_id, task_id, submission_id)
        """)

        # ── Buyer-approval columns for paid referral claims (MT-TASK-06).
        # Smallest additive change possible: approval state lives at the
        # SUBMISSION layer (never on user_tasks), and the existing
        # submission `status` vocabulary stays exactly the four MT-TASK-04
        # states — a claim is 'submitted' while approval is pending.
        #   approval_status      NULL (not approval-gated) |
        #                       'pending' | 'approved' | 'rejected'
        #   approver_user_id     server-authorized decider (on decision)
        #   approval_decided_at  decision timestamp (on decision)
        # Legacy/non-referral rows keep NULL in all three.
        try:
            conn.execute(
                "ALTER TABLE task_submissions ADD COLUMN approval_status TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute(
                "ALTER TABLE task_submissions ADD COLUMN approver_user_id INTEGER"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute(
                "ALTER TABLE task_submissions ADD COLUMN approval_decided_at TIMESTAMP"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        # ── Manual/social-proof reference (MT-TASK-15). ────────────
        # Smallest additive change possible: a bounded text/URL proof
        # reference for the manual proof task family.  NULL for every
        # non-manual submission (legacy rows included); read/written
        # ONLY through TaskSubmissionStore, and never trusted for
        # identity, authorization, reward, task or user identity.
        try:
            conn.execute(
                "ALTER TABLE task_submissions ADD COLUMN proof_ref TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Buyer view: pending claims of one task, oldest first.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_submissions_pending_approval
            ON task_submissions (task_id, approval_status, submission_id)
        """)

        # ── Wallet / ledger / withdrawal schema (MT-1) ─────────────
        # Additive migration only: no existing table or row is touched.
        # Money is stored as INTEGER units only — no REAL, no floats:
        #     1 USDT = 100,000,000 wallet units
        #     1 EGP  = 100 minor units
        # USDT is the wallet currency; EGP is display/conversion only.

        # One wallet row per user; created lazily by future wallet logic
        # (no backfill of existing users here).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                available_units INTEGER NOT NULL DEFAULT 0
                    CHECK (available_units >= 0),
                held_units INTEGER NOT NULL DEFAULT 0
                    CHECK (held_units >= 0),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Append-only ledger.  entry_type fully determines both deltas
        # (machine-checked below), and duplicates are blocked per
        # (reference_type, reference_id, entry_type) and per idempotency key.
        # Append-only write discipline itself is enforced by the future
        # Ledger service — the schema intentionally adds no triggers.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                entry_type TEXT NOT NULL CHECK (entry_type IN (
                    'credit', 'debit', 'hold', 'release', 'settlement')),
                amount_units INTEGER NOT NULL CHECK (amount_units > 0),
                available_delta INTEGER NOT NULL,
                held_delta INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USDT'
                    CHECK (currency = 'USDT'),
                reference_type TEXT NOT NULL CHECK (reference_type IN (
                    'withdrawal', 'task', 'referral', 'deposit',
                    'admin_credit', 'adjustment')),
                reference_id TEXT NOT NULL,
                idempotency_key TEXT,
                actor_user_id INTEGER,
                rate_usdt_egp TEXT,
                metadata TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (actor_user_id) REFERENCES users(user_id),
                CHECK (
                    (entry_type = 'credit'
                        AND available_delta = amount_units
                        AND held_delta = 0)
                    OR (entry_type = 'debit'
                        AND available_delta = -amount_units
                        AND held_delta = 0)
                    OR (entry_type = 'hold'
                        AND available_delta = -amount_units
                        AND held_delta = amount_units)
                    OR (entry_type = 'release'
                        AND available_delta = amount_units
                        AND held_delta = -amount_units)
                    OR (entry_type = 'settlement'
                        AND available_delta = 0
                        AND held_delta = -amount_units)
                ),
                UNIQUE (reference_type, reference_id, entry_type)
            )
        """)
        # Idempotency: one ledger entry per external event key.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_idempotency_key
            ON ledger (idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """)
        # Audit/history access path per user, newest first.
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_user
            ON ledger (user_id, id DESC)
        """)

        # Withdrawal requests: amounts in integer minor units, rates pinned
        # as canonical TEXT decimal strings (never REAL).  rate_usdt_egp may
        # be NULL for Vodafone Cash exactly as the existing withdrawal rules
        # define it; wallet_rate_usdt_egp is always required.
        # MT-ADMIN-19 additive facts (nullable at schema level — SQLite
        # cannot ADD a NOT NULL column without a default, and legacy rows
        # legitimately have none; future withdrawal code populates them):
        #   wallet_debit_units  INTEGER — exact USDT atomic units
        #       (1 USDT = 100,000,000) that a future service will
        #       reserve/settle from the wallet: the wallet/ledger
        #       financial authority.  NULL on legacy rows is NOT zero and
        #       is never derived from the amount/fee columns here.
        #   user_destination  TEXT — the USER's own payout destination
        #       (crypto address or cash-provider phone number), never
        #       payment_methods.destination (the platform destination)
        #       and never logged raw.
        #   rejected_at / completed_at  TIMESTAMP — status-transition
        #       audit stamps, NULL until the matching transition happens.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL CHECK (method IN (
                    'vodafone_cash', 'usdt_bep20')),
                amount_egp_minor INTEGER NOT NULL
                    CHECK (amount_egp_minor > 0),
                fee_egp_minor INTEGER NOT NULL CHECK (fee_egp_minor >= 0),
                amount_native_minor INTEGER NOT NULL
                    CHECK (amount_native_minor > 0),
                fee_native_minor INTEGER NOT NULL
                    CHECK (fee_native_minor >= 0),
                native_unit TEXT NOT NULL CHECK (native_unit IN ('EGP', 'USDT')),
                rate_usdt_egp TEXT,
                wallet_rate_usdt_egp TEXT NOT NULL,
                rate_captured_at TIMESTAMP NOT NULL,
                rate_provider TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'pending', 'rejected', 'completed')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payment_method_id INTEGER REFERENCES payment_methods(id),
                pm_display_name TEXT,
                pm_category TEXT,
                pm_asset TEXT,
                pm_network TEXT,
                pm_provider TEXT,
                pm_destination TEXT,
                wallet_debit_units INTEGER,
                user_destination TEXT,
                rejected_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        # Cooldown lookup (rule: one request per user per 24 hours).
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_withdrawals_user_created
            ON withdrawal_requests (user_id, created_at DESC)
        """)
        # At most one pending (open hold) withdrawal per user.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_withdrawals_one_pending
            ON withdrawal_requests (user_id)
            WHERE status = 'pending'
        """)

        # ── Withdrawal ↔ payment-method linkage (MT-ADMIN-10) ───────
        # Additive migration only: seven NULLABLE columns on
        # withdrawal_requests.  payment_method_id is a real FK to
        # payment_methods(id) (SQLite allows the inline REFERENCES
        # clause in ADD COLUMN because the default value is NULL);
        # the pm_* columns snapshot the linked method's display
        # fields at request time.  Existing rows and every
        # pre-existing column — including the method CHECK and the
        # native_unit policy — are untouched.  Re-running init_db on
        # an already-migrated database is a harmless no-op.
        for _linkage_column in (
            "payment_method_id INTEGER REFERENCES payment_methods(id)",
            "pm_display_name TEXT",
            "pm_category TEXT",
            "pm_asset TEXT",
            "pm_network TEXT",
            "pm_provider TEXT",
            "pm_destination TEXT",
        ):
            try:
                conn.execute(
                    "ALTER TABLE withdrawal_requests ADD COLUMN "
                    + _linkage_column
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # ── Withdrawal financial facts + user destination (MT-ADMIN-19) ──
        # Additive migration only: four NULLABLE columns on
        # withdrawal_requests.  wallet_debit_units is INTEGER (USDT
        # atomic units — never REAL, no default invented: SQLite cannot
        # ADD a NOT NULL column without one, and a fake default would
        # pretend legacy rows carry a debit amount).  user_destination
        # is the USER's payout destination, deliberately distinct from
        # payment_methods.destination.  rejected_at/completed_at are
        # status-transition stamps.  Legacy rows keep NULL in all four;
        # no existing column, CHECK, index or row is touched, and
        # re-running init_db on an already-migrated database is a
        # harmless no-op.
        for _facts_column in (
            "wallet_debit_units INTEGER",
            "user_destination TEXT",
            "rejected_at TIMESTAMP",
            "completed_at TIMESTAMP",
        ):
            try:
                conn.execute(
                    "ALTER TABLE withdrawal_requests ADD COLUMN "
                    + _facts_column
                )
            except sqlite3.OperationalError:
                pass  # column already exists

        # ── Linked social accounts (SA-YT-01) ────────────────────────
        # Additive migration only: a brand-new table — no existing table
        # or row is touched.  The stable provider identity (YouTube: the
        # channel id) is the link identity, never a display name.
        # OAuth tokens live here only in encrypted form (see
        # social_accounts.TokenCipher); no client secrets, no passwords.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS social_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                username TEXT,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'linked'
                    CHECK (status IN ('linked', 'revoked')),
                access_token_encrypted TEXT,
                refresh_token_encrypted TEXT,
                token_expires_at TIMESTAMP,
                scopes TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        # A YouTube channel belongs to at most one Telegram account.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_social_accounts_provider_user
            ON social_accounts (provider, provider_user_id)
        """)
        # One active link per (user, provider): no duplicate YouTube links.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_social_accounts_user_provider
            ON social_accounts (user_id, provider)
            WHERE status = 'linked'
        """)

        # ── Administrative administrators (MT-ADMIN-37) ─────────────
        # Additive migration only: brand-new table.  THE persistent
        # admin registry the Control Center's admins module manages;
        # config.is_admin() consults it (via db.is_admin_user) after
        # the configured bootstrap list — the SAME union this module's
        # list surface renders.
        #   user_id   Telegram numeric id — the ONLY identity key
        #   active    1 = current administrator; removal is a soft
        #             state flip so audit history is never destroyed
        #   added_by  the administrator id that added this record
        #             (NULL = configured/bootstrap record)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                added_by INTEGER
            )
        """)
        # Deterministic, idempotent bootstrap: every configured
        # config.ADMINS id gets its authoritative row AND an active
        # state (INSERT OR IGNORE + UPDATE — repeated init_db() runs
        # can never duplicate a record, and a configured admin is
        # always active after initialization).  A configured admin
        # would stay authorized even if this sync were skipped,
        # because config.is_admin checks the configured list FIRST.
        for bootstrap_id in ADMINS:
            conn.execute(
                "INSERT OR IGNORE INTO admin_users "
                "(user_id, active, added_by) VALUES (?, 1, NULL)",
                (bootstrap_id,),
            )
            conn.execute(
                "UPDATE admin_users SET active = 1 WHERE user_id = ?",
                (bootstrap_id,),
            )

        # ── Admin broadcast (MT-ADMIN-38) ─────────────────────────
        # Additive migration only: brand-new table (idempotent
        # CREATE TABLE IF NOT EXISTS — pre-existing databases upgrade
        # in place, nothing is dropped or rewritten, no financial
        # schema is touched).  The Control Center's broadcast module
        # persists its ENTIRE state here: draft ownership, bounded
        # message, explicit status, timestamps and the completion
        # counts.  The atomic draft→sending UPDATE is the
        # duplicate-send authority (see the store operations above).
        #   admin_user_id  the authorized actor (NOT FK'd to
        #                  admin_users: authorization is decided by
        #                  config.is_admin at write time and this is
        #                  not a user registry)
        #   message        '' while composing; bounded by Telegram's
        #                  4096-char text limit (CHECK below)
        #   status         'draft' | 'sending' | 'completed' |
        #                  'cancelled' (explicit CHECK constraint)
        #   recipient/success/failure_count  stamped by the winning
        #                  pass (S + F = N by construction)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN (
                        'draft', 'sending', 'completed', 'cancelled'
                    )),
                recipient_count INTEGER,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP,
                completed_at TIMESTAMP,
                CHECK (length(message) <= 4096)
            )
        """)
        # At most ONE open draft per administrator — the same partial
        # -index pattern as admin_task_drafts, so confirm/cancel
        # payloads need no identifier (and never the message body).
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_broadcasts_open
            ON broadcasts (admin_user_id) WHERE status = 'draft'
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_broadcasts_admin
            ON broadcasts (admin_user_id, id)
        """)

        # ── Admin notification linkage (MT-ADMIN-03) ───────────────
        # Additive migration only: a brand-new table.  Persists the
        # association between a server-side operation (e.g. a manual
        # proof claim) and the Telegram admin message that was sent
        # for it, so untrusted callback data can always be resolved
        # back to server-side state.  No in-memory operation state.
        # Deliberately generic (operation_type + operation_id): the
        # table does not FK to task_submissions so future operation
        # families can reuse it without schema changes.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                operation_id INTEGER NOT NULL,
                admin_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Exactly ONE notification message per operation per admin
        # chat — the notification path is idempotent under replay.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_notifications_op_chat
            ON admin_notifications (operation_type, operation_id, admin_chat_id)
        """)

        # ── Admin task-creation drafts (MT-ADMIN-05) ──────────────
        # Additive migration only: a brand-new table.  The /addtask
        # wizard persists its current step + payload JSON here so a
        # draft survives bot restarts, handler recreation and process
        # failures between steps — there is no in-memory wizard state
        # anywhere.  A draft belongs to exactly one admin (every read/
        # write is filtered by admin_user_id) and at most ONE draft per
        # admin can be open at a time (partial unique index below).
        #   status          'open' (being edited) | 'published'
        #                   (a task was created from it; the confirm
        #                   CAS below makes replays idempotent)
        #   published_task_id  the tasks row created on publish
        # Cancelling simply deletes the draft row.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_task_drafts (
                draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                step TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'published')),
                payload_json TEXT NOT NULL,
                published_task_id INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_task_drafts_open
            ON admin_task_drafts (admin_user_id) WHERE status = 'open'
        """)

        # ── Persistent Telegram user support (MT-ADMIN-06) ─────────
        # Additive migration only: brand-new tables.  A support
        # inquiry is ONE active conversation between one Telegram user
        # and the admins; every message is stored forever, the user's
        # in-progress category selection and the admin's reply context
        # also live here.  There is NO in-memory conversation state
        # anywhere, so a bot restart keeps inquiries, messages,
        # status, admin/user linkage and pending reply context.
        #   status  'open' (active) | 'closed' (terminal)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # DB-enforced one active inquiry per user: closed inquiries do
        # not count, so a user may always start one new thread later.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_support_inquiries_active_user
            ON support_inquiries (user_id) WHERE status != 'closed'
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_support_inquiries_status
            ON support_inquiries (status, id)
        """)
        # Every support message is stored exactly once, in order.
        #   sender_type   'user' | 'admin'
        #   source_message_id  the Telegram message id of the sender's
        #                   private chat — NULL for synthetic rows;
        #                   the UNIQUE index below makes a replayed
        #                   Telegram update insert nothing twice
        #   delivered_at  admin→user delivery marker (NULL = preserved
        #                 but NOT delivered yet — never faked)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL
                    REFERENCES support_inquiries(id) ON DELETE CASCADE,
                sender_type TEXT NOT NULL
                    CHECK (sender_type IN ('user', 'admin')),
                sender_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                source_message_id INTEGER,
                delivered_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_support_messages_inquiry
            ON support_messages (inquiry_id, id)
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_support_messages_source
            ON support_messages
                (sender_type, sender_id, source_message_id)
            WHERE source_message_id IS NOT NULL
        """)
        # User-side flow state: /support → category → message.  Lives
        # in SQLite (not handler memory) so the flow survives restart.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_user_states (
                user_id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Admin reply context: WHICH inquiry an admin is answering.
        # One active context per admin, resolved from DB by admin id +
        # private chat id — never from client-provided data.  Closing
        # an inquiry deletes the contexts that point at it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_reply_contexts (
                admin_user_id INTEGER PRIMARY KEY,
                admin_chat_id INTEGER NOT NULL,
                inquiry_id INTEGER NOT NULL
                    REFERENCES support_inquiries(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Dynamic payment methods (MT-ADMIN-08) ─────────────────
        # Additive migration only: a brand-new table.  Admin-defined
        # payout destinations — provider, network, asset and the
        # destination itself are FREE-FORM TEXT so a new exchange,
        # chain or cash provider never needs a migration.  Only
        # category is a closed taxonomy (crypto | cash: the two
        # payment concepts), never a provider/network list.
        #   is_active     admin toggle (0/1); inactive = hidden from
        #                 users but preserved
        #   sort_order    display ordering (defaults to insertion
        #                 order; see payment_method_store)
        #   created_by / updated_by   admin audit columns (no FK: an
        #                 admin may not have a users row yet)
        # NO provider addresses are seeded here — addresses are
        # runtime admin configuration, never source constants.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL
                    CHECK (category IN ('crypto', 'cash')),
                display_name TEXT NOT NULL,
                asset TEXT NOT NULL,
                network TEXT,
                provider TEXT NOT NULL,
                destination TEXT NOT NULL,
                instructions TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1)),
                deposits_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (deposits_enabled IN (0, 1)),
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                updated_by INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ── Deposit availability (MT-ADMIN-28) ──────────────────────
        # Additive migration only: ONE new column on payment_methods.
        # `category` is the crypto/cash payment concept and is NOT
        # overloaded — deposit availability is its own explicit flag,
        # defaulting to 0 so NO existing method silently becomes a
        # deposit method.  Existing rows/columns and every pre-existing
        # CHECK are untouched; re-running init_db is a harmless no-op.
        try:
            conn.execute(
                "ALTER TABLE payment_methods ADD COLUMN "
                "deposits_enabled INTEGER NOT NULL DEFAULT 0 "
                "CHECK (deposits_enabled IN (0, 1))"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # Deterministic display order (sort_order ASC, id ASC).
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_payment_methods_order
            ON payment_methods (sort_order, id)
        """)

        # ── User deposit intents (MT-ADMIN-28) ──────────────────────
        # Additive migration only: a brand-new table — no existing
        # table, column or row is touched.  This is the deposit
        # REQUEST/instruction record only: creating a row never moves
        # money.  amount_units is integer USDT atomic units (never
        # REAL, never 2-decimal accounting).  status starts at
        # 'pending' (unverified) from the user flow and may only
        # become 'credited'/'rejected' through a future authoritative
        # verification event — no such source exists yet.  The pm_*
        # columns snapshot the configured method's user-relevant facts
        # (including the PLATFORM deposit destination) at request time
        # so later admin edits never rewrite historical instructions.
        # external_tx_id is the future idempotency key: NULL now —
        # never fabricated — with a partial UNIQUE index so one
        # external transaction can never be credited twice.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposit_requests (
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                payment_method_id INTEGER NOT NULL,
                amount_units INTEGER NOT NULL CHECK (amount_units > 0),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'credited', 'rejected')),
                pm_display_name TEXT NOT NULL,
                pm_asset TEXT NOT NULL,
                pm_network TEXT,
                pm_provider TEXT NOT NULL,
                pm_destination TEXT NOT NULL,
                external_tx_id TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (payment_method_id) REFERENCES payment_methods(id)
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_deposit_requests_tx
            ON deposit_requests (external_tx_id)
            WHERE external_tx_id IS NOT NULL
        """)

        # ── Manual deposit proof evidence (MT-ADMIN-31) ──────────────
        # Additive migration only: a brand-new EVIDENCE table — no
        # existing table, column, row, financial CHECK constraint or
        # unique index is touched.  A proof row associates an
        # admin-reviewable screenshot reference with a deposit
        # REQUEST; it is never a financial fact: submitting a proof
        # never changes deposit_requests.status (still exactly
        # pending/credited/rejected) and no credit path exists here.
        # storage_key is a server-generated relative filename — image
        # bytes live on disk (never in SQLite) and client filenames
        # are never stored.  The partial UNIQUE index allows at most
        # ONE pending-review proof per request, so a repeat upload is
        # a deterministic business response while a reviewed proof
        # (approved/rejected evidence) is never overwritten or
        # deleted.  reviewed_by/reviewed_at record WHO decided and
        # WHEN; review_note optionally records a safe reason.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposit_proofs (
                proof_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                storage_key TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                width INTEGER,
                height INTEGER,
                status TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (status IN
                        ('pending_review', 'approved', 'rejected')),
                review_note TEXT,
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (request_id)
                    REFERENCES deposit_requests(request_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_deposit_proofs_pending
            ON deposit_proofs (request_id)
            WHERE status = 'pending_review'
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_deposit_proofs_status
            ON deposit_proofs (status, created_at)
        """)

        # ── Admin platform settings (MT-ADMIN-15) ─────────────────
        # Additive migration only: a brand-new table — no existing
        # table, column or row is touched, and no financial data is
        # rewritten.  The platform's genuinely mutable business knobs
        # (minimum withdrawal, minimum deposit, withdrawal fee,
        # advertiser commission) become DATA instead of constants:
        #   key      stable string key; the registry of valid keys,
        #            bounds and defaults lives in ``platform_settings``
        #   value    the EXACT integer representation of the setting:
        #            USDT atomic units for the ``*_units`` keys
        #            (1 USDT = 100,000,000 units, 8 dp — sub-cent
        #            values are exact) and integer basis points for
        #            ``advertiser_commission`` (10,000 bp = 100 %).
        #            The ``typeof(value) = 'integer'`` CHECK keeps a
        #            REAL/float out of the table permanently; there is
        #            deliberately no REAL column anywhere in the
        #            schema (money is never a float).
        #   updated_by  acting admin id — same no-FK convention as
        #            payment_methods.created_by / updated_by
        # Re-running init_db on a fresh or an already-migrated database
        # is a harmless no-op (IF NOT EXISTS + INSERT OR IGNORE), and
        # an admin-saved value is never overwritten by a migration.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS platform_settings (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL CHECK (typeof(value) = 'integer'),
                updated_by INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ── MT-ADMIN-26: authoritative manual EGP-per-USDT rate ──────
        # A DEDICATED singleton table — deliberately NOT
        # platform_settings (integer-only by contract: fractional
        # rates have no business there) and no REAL column anywhere.
        #   id          singleton identity: exactly one row, id = 1
        #   rate_usdt_egp  canonical plain-decimal TEXT (rate_quote
        #               contract: "48", "48.5", "48.5001" — never
        #               scientific notation, never a float)
        #   provider    approved source id ("manual" only today)
        #   captured_at server-generated aware UTC ISO-8601 instant
        #               the rate applies from
        #   updated_by  acting admin Telegram id (no-FK convention,
        #               same as payment_methods/platform_settings)
        #   updated_at  server-generated aware UTC ISO-8601 instant of
        #               the last replace
        # Additive + startup-safe: CREATE TABLE IF NOT EXISTS preserves
        # an existing row on every restart; re-running init_db never
        # overwrites the admin's current rate.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS current_rate (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                rate_usdt_egp TEXT NOT NULL,
                provider TEXT NOT NULL,
                captured_at TIMESTAMP NOT NULL,
                updated_by INTEGER NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Seed the audited defaults for keys that have no row yet.
        # The key -> default registry is the single source of truth in
        # ``platform_settings``; the import is function-local on purpose
        # so db <-> platform_settings can never become an import cycle
        # (platform_settings imports db at module level for its
        # connection/transaction helpers).
        from platform_settings import ensure_default_settings
        ensure_default_settings(conn)

        logger.info("Database initialized: %s", db_path or DB_PATH)


# ── Authoritative user administration reads (MT-ADMIN-35) ────────────
# The ONLY user-listing interfaces in the repository.  Read-only,
# parameterized, deterministically ordered and hard-bounded — the
# Telegram admin control center builds on these and issues no SQL of
# its own.  No wallet, ledger, destination or secret column is ever
# selected here.

# Hard upper bound for any single users read: no caller can ever ask
# the store for an unbounded page, regardless of what it passes.
MAX_USER_LIST_LIMIT = 100


def count_users(db_path: str | None = None) -> int:
    """Total registered users — the ONE authoritative user count.

    Read-only COUNT over the ``users`` table.  Both the ``/control``
    dashboard metric and the admin user-management module read this
    same function, so no cached or duplicated count can drift.
    """
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return row["count"]


def list_users(
    limit: int, offset: int = 0, db_path: str | None = None
) -> list[dict]:
    """One bounded page of users in deterministic order (read-only).

    Ordering is ``user_id DESC``: Telegram user ids grow over time, so
    this is newest-registration-first with a unique, indexed
    tiebreaker — repeated calls return the identical order.  Only the
    safe identity columns (user_id / username / first_name) are
    selected; never language, referrals, or anything financial.

    Bounds: ``limit`` is clamped to ``MAX_USER_LIST_LIMIT`` and a
    non-positive page (or negative offset) resolves to an empty list,
    so no caller can trigger an unbounded read.  Type errors are
    raised instead of being silently coerced — bools included.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an int")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an int")
    limit = min(limit, MAX_USER_LIST_LIMIT)
    if limit <= 0 or offset < 0:
        return []
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT user_id, username, first_name FROM users "
            "ORDER BY user_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def list_broadcast_recipient_ids(db_path: str | None = None) -> list[int]:
    """Every registered user id — the ONLY recipient source of a
    broadcast (MT-ADMIN-38).

    Straight from the authoritative ``users`` table: never
    ``config.ADMINS``, never task participants, wallet holders,
    withdrawal/deposit users, username search or any cached list.
    id-only (no username/identity/financial column is selected),
    deterministically ordered, and FAILS CLOSED with ValueError when
    the population exceeds ``MAX_BROADCAST_RECIPIENTS`` so a runaway
    table is never pulled into memory and a broadcast above the
    bound can never start silently partial.  Read-only: never
    mutates the users table.
    """
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        if row["count"] > MAX_BROADCAST_RECIPIENTS:
            raise ValueError("broadcast recipient population too large")
        cursor = conn.execute(
            "SELECT user_id FROM users ORDER BY user_id ASC"
        )
        return [int(r["user_id"]) for r in cursor.fetchall()]


def get_withdrawal_request(
    request_id: str, db_path: str | None = None
) -> dict | None:
    """Read one withdrawal request by id (MT-ADMIN-19 read mapping).

    Returns every stored column — including the additive facts
    ``wallet_debit_units`` (USDT atomic units; NULL on legacy rows and
    never reinterpreted), ``user_destination``, ``rejected_at`` and
    ``completed_at`` — or None when the id is unknown.

    Read-only mapping helper: no repository, status-transition or
    money logic lives here (a future withdrawal service owns those).
    ``user_destination`` is the USER's payout destination; it is never
    logged raw and never merged with the platform's
    ``payment_methods.destination``.
    """
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM withdrawal_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def load_channels(db_path: str | None = None) -> None:
    """Load all required channels from SQLite into the in-memory CHANNELS dict."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title, required, chat_type FROM required_channels"
        )
        rows = cursor.fetchall()

        CHANNELS.clear()
        for row in rows:
            CHANNELS[row["slug"]] = Channel(
                slug=row["slug"],
                channel_id=row["channel_id"],
                username=row["username"],
                title=row["title"],
                required=bool(row["required"]),
                chat_type=row["chat_type"] if row["chat_type"] else "channel",
            )

        logger.info("Loaded %d channels from database", len(rows))


def save_channel(channel: Channel, db_path: str | None = None) -> None:
    """Persist a channel to the SQLite database."""
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO required_channels 
               (slug, channel_id, username, title, required, chat_type)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel.slug, channel.channel_id, channel.username,
             channel.title, int(channel.required), channel.chat_type)
        )
        logger.info("Channel saved to DB: %s", channel.slug)


def delete_channel(slug: str, db_path: str | None = None) -> None:
    """Remove a channel from the SQLite database."""
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM required_channels WHERE slug = ?", (slug,))
        logger.info("Channel deleted from DB: %s", slug)


def get_channel_from_db(slug: str, db_path: str | None = None) -> Optional[Channel]:
    """Retrieve a single channel from the database."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title, required, chat_type FROM required_channels WHERE slug = ?",
            (slug,)
        )
        row = cursor.fetchone()
        if row:
            return Channel(
                slug=row["slug"], channel_id=row["channel_id"], username=row["username"],
                title=row["title"], required=bool(row["required"]), chat_type=row["chat_type"] if row["chat_type"] else "channel",
            )
        return None


# ── Task Definitions ──────────────────────────────────────────────


def validate_repeat_policy(repeat_policy, repeat_hours) -> tuple[str, int | None]:
    """Validate and normalize a task repeat-policy pair (MT-TASK-04).

    Rules (server-side only — never client-provided):
        - repeat_policy must be 'one_time' or 'repeatable'
        - one_time does not use repeat_hours (must be None)
        - repeatable requires an integer repeat_hours >= 1
        - floats, booleans, negatives and zero are rejected

    Returns the normalized (repeat_policy, repeat_hours) pair.
    Raises ValueError for any invalid combination.
    """
    if repeat_policy is None:
        repeat_policy = REPEAT_POLICY_ONE_TIME
    if not isinstance(repeat_policy, str) or repeat_policy not in REPEAT_POLICIES:
        raise ValueError(f"invalid repeat_policy: {repeat_policy!r}")
    if repeat_hours is not None:
        if isinstance(repeat_hours, bool) or not isinstance(repeat_hours, int):
            raise ValueError("repeat_hours must be an integer")
    if repeat_policy == REPEAT_POLICY_ONE_TIME:
        if repeat_hours is not None:
            raise ValueError("repeat_hours is only valid for repeatable tasks")
    else:
        if repeat_hours is None or repeat_hours < 1:
            raise ValueError("repeatable tasks require repeat_hours >= 1")
    return repeat_policy, repeat_hours


def create_task(title: str, description: str, task_type: str, reward: int,
                active: bool = True, db_path: str | None = None,
                task_data: str | None = None,
                repeat_policy: str = REPEAT_POLICY_ONE_TIME,
                repeat_hours: int | None = None,
                conn: sqlite3.Connection | None = None,
                reward_units: int | None = None,
                commission_units: int | None = None) -> int:
    """Create a new task definition. Returns the new task ID.

    Args:
        task_data: Optional JSON string with task-specific verification data.
        repeat_policy: 'one_time' (default) or 'repeatable'.
        repeat_hours: Required integer >= 1 for repeatable tasks only.
        conn: Optional caller-owned connection (MT-ADMIN-05 publish
            transaction).  When given, the INSERT runs on THAT
            connection inside the caller's transaction and is NOT
            committed here; ownership, BEGIN/COMMIT and rollback stay
            entirely with the caller (``db.transaction()``).  When
            None, the classic self-contained ``get_connection()``
            scope is used — existing callers are unaffected.
        reward_units: Optional EXACT atomic reward (MT-ADMIN-14) from
            the canonical reward parser — stored verbatim as the
            accounting authority.  When None, the MT-ADMIN-13
            whole-USDT derivation from ``reward`` still applies.
        commission_units: Optional EXACT atomic advertiser-commission
            snapshot (MT-ADMIN-16) resolved by the canonical creation
            service from the runtime platform setting — stored
            VERBATIM, never derived, never recomputed.  When None
            (legacy/direct callers, pre-MT-ADMIN-16 rows) the column
            stays NULL; nothing is invented.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty")
    if not description or not description.strip():
        raise ValueError("description cannot be empty")
    if not task_type or not task_type.strip():
        raise ValueError("type cannot be empty")
    if reward < 0:
        raise ValueError("reward cannot be negative")
    # MT-ADMIN-13/14: the authoritative atomic reward alongside the
    # whole-USDT display value — exact Python int math only.  An
    # explicit ``reward_units`` (from the canonical reward parser) is
    # validated and stored VERBATIM, never re-derived from ``reward``.
    # Without it, a non-integer reward (reachable only by direct
    # callers; the app layer validates ints) stores NULL so
    # settlement's legacy validation rejects it exactly as it did
    # before this column existed.
    if reward_units is None:
        reward_units = (
            reward * wallet.USDT_SCALE
            if isinstance(reward, int) and not isinstance(reward, bool)
            else None
        )
    elif (
        isinstance(reward_units, bool)
        or not isinstance(reward_units, int)
        or reward_units < 0
        or reward_units > _SQLITE_INT64_MAX
    ):
        raise ValueError("reward_units must be a non-negative int64")
    # MT-ADMIN-16: the commission snapshot is an exact non-negative
    # int64 of atomic units — validated, never coerced, never rounded.
    if commission_units is not None and (
        isinstance(commission_units, bool)
        or not isinstance(commission_units, int)
        or commission_units < 0
        or commission_units > _SQLITE_INT64_MAX
    ):
        raise ValueError("commission_units must be a non-negative int64")
    repeat_policy, repeat_hours = validate_repeat_policy(
        repeat_policy, repeat_hours
    )

    insert_sql = (
        "INSERT INTO tasks "
        "(title, description, type, reward, active, task_data, "
        " repeat_policy, repeat_hours, reward_units, commission_units) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    values = (title.strip(), description.strip(), task_type.strip(), reward,
              int(active), task_data, repeat_policy, repeat_hours,
              reward_units, commission_units)

    if conn is not None:
        cursor = conn.execute(insert_sql, values)
        task_id = cursor.lastrowid
        logger.info("Task created: id=%d title=%s", task_id, title)
        return task_id

    with get_connection(db_path) as own_conn:
        cursor = own_conn.execute(insert_sql, values)
        task_id = cursor.lastrowid
        logger.info("Task created: id=%d title=%s", task_id, title)
        return task_id


def get_task(task_id: int, db_path: str | None = None) -> dict | None:
    """Get a task by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, description, type, reward, reward_units, "
            "commission_units, active, task_data, repeat_policy, "
            "repeat_hours, created_at "
            "FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        if row:
            return {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "type": row["type"],
                "reward": row["reward"],
                "reward_units": row["reward_units"],
                "commission_units": row["commission_units"],
                "active": bool(row["active"]),
                "task_data": row["task_data"],
                "repeat_policy": row["repeat_policy"],
                "repeat_hours": row["repeat_hours"],
                "created_at": row["created_at"],
            }
        return None


def list_tasks(active_only: bool = False, db_path: str | None = None) -> list[dict]:
    """List all tasks, optionally filtering to active only."""
    with get_connection(db_path) as conn:
        columns = (
            "id, title, description, type, reward, reward_units, "
            "commission_units, active, "
            "task_data, repeat_policy, repeat_hours, created_at"
        )
        if active_only:
            cursor = conn.execute(
                f"SELECT {columns} FROM tasks WHERE active = 1"
            )
        else:
            cursor = conn.execute(f"SELECT {columns} FROM tasks")
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "type": row["type"],
                "reward": row["reward"],
                "reward_units": row["reward_units"],
                "commission_units": row["commission_units"],
                "active": bool(row["active"]),
                "task_data": row["task_data"],
                "repeat_policy": row["repeat_policy"],
                "repeat_hours": row["repeat_hours"],
                "created_at": row["created_at"],
            }
            for row in cursor.fetchall()
        ]


def update_task(task_id: int, title: str | None = None, description: str | None = None,
                task_type: str | None = None, reward: int | None = None,
                active: bool | None = None, db_path: str | None = None,
                repeat_policy: str | None = None,
                repeat_hours: int | None = None) -> bool:
    """Update a task. Returns True if the task existed and was updated.

    ``repeat_policy`` / ``repeat_hours`` are validated as a merged pair
    with the current row, so a partially-specified repeat change (e.g.
    switching to repeatable without hours) is rejected.
    """
    if title is not None and (not title or not title.strip()):
        raise ValueError("title cannot be empty")
    if description is not None and (not description or not description.strip()):
        raise ValueError("description cannot be empty")
    if task_type is not None and (not task_type or not task_type.strip()):
        raise ValueError("type cannot be empty")
    if reward is not None and reward < 0:
        raise ValueError("reward cannot be negative")

    fields = []
    values = []
    if title is not None:
        fields.append("title = ?")
        values.append(title.strip())
    if description is not None:
        fields.append("description = ?")
        values.append(description.strip())
    if task_type is not None:
        fields.append("type = ?")
        values.append(task_type.strip())
    if reward is not None:
        fields.append("reward = ?")
        values.append(reward)
        # MT-ADMIN-13: keep the authoritative atomic value in step with
        # an edited whole-USDT reward (exact int math; a non-integer
        # reward clears the column to NULL so settlement's legacy
        # validation rejects the row exactly as before).
        fields.append("reward_units = ?")
        values.append(
            reward * wallet.USDT_SCALE
            if isinstance(reward, int) and not isinstance(reward, bool)
            else None
        )
    if active is not None:
        fields.append("active = ?")
        values.append(int(active))
    if repeat_policy is not None or repeat_hours is not None:
        current = get_task(task_id, db_path)
        base_policy = (
            repeat_policy if repeat_policy is not None
            else (current["repeat_policy"] if current else REPEAT_POLICY_ONE_TIME)
        )
        base_hours = (
            repeat_hours if repeat_hours is not None
            else (current["repeat_hours"] if current else None)
        )
        base_policy, base_hours = validate_repeat_policy(
            base_policy, base_hours
        )
        fields.append("repeat_policy = ?")
        values.append(base_policy)
        fields.append("repeat_hours = ?")
        values.append(base_hours)

    if not fields:
        return False

    values.append(task_id)
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
            values
        )
        return cursor.rowcount > 0


def delete_task(task_id: int, db_path: str | None = None) -> bool:
    """Delete a task. Returns True if the task existed."""
    with get_connection(db_path) as conn:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0


# ── User Task State ──────────────────────────────────────────────


def create_user_task(user_id: int, task_id: int, db_path: str | None = None) -> bool:
    """Create a user_task row with status 'available'.

    Returns True on success.
    Raises ValueError if user/task don't exist or status is invalid.
    Raises sqlite3.IntegrityError on duplicate (user_id, task_id).
    """
    with get_connection(db_path) as conn:
        # Validate user exists
        if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
            raise ValueError(f"user_id {user_id} does not exist")
        # Validate task exists
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            raise ValueError(f"task_id {task_id} does not exist")

        conn.execute(
            "INSERT INTO user_tasks (user_id, task_id, status) VALUES (?, ?, ?)",
            (user_id, task_id, USER_TASK_STATUS_AVAILABLE)
        )
        return True


def get_user_task(user_id: int, task_id: int, db_path: str | None = None) -> dict | None:
    """Get a single user_task row."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, task_id, status, started_at, completed_at "
            "FROM user_tasks WHERE user_id = ? AND task_id = ?",
            (user_id, task_id)
        ).fetchone()
        if row:
            return {
                "user_id": row["user_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            }
        return None


def update_user_task_status(user_id: int, task_id: int, new_status: str,
                           db_path: str | None = None,
                           _allow_completion: bool = False) -> bool:
    """Update user_task status with transition validation.

    Allowed transitions:
        available → started           (always allowed)
        started   → completed         (only via CompletionGate)

    The ``_allow_completion`` flag is an internal guard: only the
    CompletionGate in ``task_completion.py`` passes ``True``.  Any
    other caller attempting ``started → completed`` will get a
    ``ValueError``.

    Returns True if updated, False if row not found.
    Raises ValueError for invalid status or illegal transition.
    """
    if new_status not in ALLOWED_USER_TASK_STATUSES:
        raise ValueError(f"invalid status: {new_status}")

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM user_tasks WHERE user_id = ? AND task_id = ?",
            (user_id, task_id)
        ).fetchone()
        if not row:
            return False

        current = row["status"]

        # Validate transition
        if current == USER_TASK_STATUS_AVAILABLE and new_status != USER_TASK_STATUS_STARTED:
            raise ValueError(
                f"cannot transition from '{current}' to '{new_status}'; "
                f"must go to '{USER_TASK_STATUS_STARTED}' first"
            )
        if current == USER_TASK_STATUS_STARTED and new_status != USER_TASK_STATUS_COMPLETED:
            raise ValueError(
                f"cannot transition from '{current}' to '{new_status}'; "
                f"only '{USER_TASK_STATUS_COMPLETED}' is allowed"
            )
        if current == USER_TASK_STATUS_STARTED and new_status == USER_TASK_STATUS_COMPLETED and not _allow_completion:
            raise ValueError(
                "started → completed is restricted to the CompletionGate; "
                "pass _allow_completion=True from within task_completion.py"
            )
        if current == USER_TASK_STATUS_COMPLETED:
            raise ValueError(f"task already completed; no further transitions allowed")

        # Set timestamps based on new status
        if new_status == USER_TASK_STATUS_STARTED:
            conn.execute(
                "UPDATE user_tasks SET status = ?, started_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND task_id = ?",
                (new_status, user_id, task_id)
            )
        elif new_status == USER_TASK_STATUS_COMPLETED:
            conn.execute(
                "UPDATE user_tasks SET status = ?, completed_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND task_id = ?",
                (new_status, user_id, task_id)
            )
        return True


def list_user_tasks(user_id: int, db_path: str | None = None) -> list[dict]:
    """List all user_tasks for a given user."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "SELECT user_id, task_id, status, started_at, completed_at "
            "FROM user_tasks WHERE user_id = ?",
            (user_id,)
        )
        return [
            {
                "user_id": row["user_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
            }
            for row in cursor.fetchall()
        ]


# ── User & Referral Attribution ───────────────────────────────────


def register_user(user_id: int, username: str | None, first_name: str | None, referred_by: int | None = None) -> bool:
    """
    Register a new user with referral attribution.

    Returns True if the user was newly created, False if already exists.

    Rules:
    - First referrer wins (no changes after initial registration)
    - Self-referral is blocked
    - Duplicate registrations are idempotent
    """
    # Check if user already exists
    existing = get_user(user_id)
    if existing:
        return False  # User already exists, no changes

    # Block self-referral
    if referred_by == user_id:
        referred_by = None

    with get_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, first_name, referred_by) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, referred_by)
            )
        except sqlite3.OperationalError:
            # users table does not exist — silently ignore
            logger.debug("users table missing, skipping register_user for %d", user_id)

    return True


def get_user(user_id: int) -> dict | None:
    """Get user by ID."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, username, first_name, referred_by, created_at FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if row:
                return {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "first_name": row["first_name"],
                    "referred_by": row["referred_by"],
                    "created_at": row["created_at"],
                }
            return None
    except sqlite3.OperationalError:
        # users table does not exist — treat as no user found
        return None


def is_supported_language(language: object) -> bool:
    """True when *language* is one of the supported language codes."""
    return isinstance(language, str) and language in SUPPORTED_LANGUAGES


def get_user_language(user_id: int) -> str | None:
    """Return the persisted language for a user, or None.

    A missing table, a missing user row, a NULL value, and an unsupported
    stored value are all safely treated as "no language selected".
    """
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT language FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        # users table does not exist — treat as no language
        return None

    if not row:
        return None
    language = row["language"]
    return language if is_supported_language(language) else None


def set_user_language(user_id: int, language: str) -> bool:
    """Persist *language* for an existing user.  Returns True on success.

    - Unsupported codes are rejected without writing anything.
    - Unknown users are rejected without creating rows (user creation
      stays exclusively with register_user, so referral attribution and
      the self-referral guard are unaffected).
    - Repeated calls with the same value are idempotent.
    """
    if not is_supported_language(language):
        return False
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return False  # unknown user — never create rows here
            conn.execute(
                "UPDATE users SET language = ? WHERE user_id = ?",
                (language, user_id),
            )
            return True
    except sqlite3.OperationalError:
        return False


def get_referrer(user_id: int) -> dict | None:
    """Get referrer for a user."""
    user = get_user(user_id)
    if user and user["referred_by"]:
        return get_user(user["referred_by"])
    return None


def get_referral_count(user_id: int) -> int:
    """Get number of users referred by this user."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as count FROM users WHERE referred_by = ?",
            (user_id,)
        ).fetchone()
        return row["count"]
