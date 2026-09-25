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

from config import Channel, CHANNELS

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
            conn.execute("ALTER TABLE tasks ADD COLUMN repeat_hours INTEGER")
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

        logger.info("Database initialized: %s", db_path or DB_PATH)


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
                repeat_hours: int | None = None) -> int:
    """Create a new task definition. Returns the new task ID.

    Args:
        task_data: Optional JSON string with task-specific verification data.
        repeat_policy: 'one_time' (default) or 'repeatable'.
        repeat_hours: Required integer >= 1 for repeatable tasks only.
    """
    if not title or not title.strip():
        raise ValueError("title cannot be empty")
    if not description or not description.strip():
        raise ValueError("description cannot be empty")
    if not task_type or not task_type.strip():
        raise ValueError("type cannot be empty")
    if reward < 0:
        raise ValueError("reward cannot be negative")
    repeat_policy, repeat_hours = validate_repeat_policy(
        repeat_policy, repeat_hours
    )

    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO tasks "
            "(title, description, type, reward, active, task_data, "
            " repeat_policy, repeat_hours) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title.strip(), description.strip(), task_type.strip(), reward,
             int(active), task_data, repeat_policy, repeat_hours)
        )
        task_id = cursor.lastrowid
        logger.info("Task created: id=%d title=%s", task_id, title)
        return task_id


def get_task(task_id: int, db_path: str | None = None) -> dict | None:
    """Get a task by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, description, type, reward, active, task_data, "
            "repeat_policy, repeat_hours, created_at FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()
        if row:
            return {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "type": row["type"],
                "reward": row["reward"],
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
            "id, title, description, type, reward, active, task_data, "
            "repeat_policy, repeat_hours, created_at"
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
