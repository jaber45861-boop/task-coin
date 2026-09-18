"""
SQLite persistence for required channels and user/referral tracking.

This module provides a thin layer over SQLite to store and load
the required-channel configuration and user referral data.
"""

import sqlite3
import os
import logging
from typing import Optional
from contextlib import contextmanager

from config import Channel, CHANNELS

logger = logging.getLogger(__name__)

# ── Allowed user_task statuses ─────────────────────────────────────
USER_TASK_STATUS_AVAILABLE = "available"
USER_TASK_STATUS_STARTED = "started"
USER_TASK_STATUS_COMPLETED = "completed"
ALLOWED_USER_TASK_STATUSES = {
    USER_TASK_STATUS_AVAILABLE,
    USER_TASK_STATUS_STARTED,
    USER_TASK_STATUS_COMPLETED,
}

DB_PATH = os.environ.get("TASKCOIN_DB_PATH", "task_coin.db")


@contextmanager
def get_connection(db_path: str | None = None):
    """Get a SQLite connection with WAL mode for better concurrency."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referred_by) REFERENCES users(user_id)
            )
        """)

        # ── Task definitions table ─────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                type TEXT NOT NULL,
                reward INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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


def create_task(title: str, description: str, task_type: str, reward: int, active: bool = True, db_path: str | None = None) -> int:
    """Create a new task definition. Returns the new task ID."""
    if not title or not title.strip():
        raise ValueError("title cannot be empty")
    if not description or not description.strip():
        raise ValueError("description cannot be empty")
    if not task_type or not task_type.strip():
        raise ValueError("type cannot be empty")
    if reward < 0:
        raise ValueError("reward cannot be negative")

    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, description, type, reward, active) VALUES (?, ?, ?, ?, ?)",
            (title.strip(), description.strip(), task_type.strip(), reward, int(active))
        )
        task_id = cursor.lastrowid
        logger.info("Task created: id=%d title=%s", task_id, title)
        return task_id


def get_task(task_id: int, db_path: str | None = None) -> dict | None:
    """Get a task by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, description, type, reward, active, created_at FROM tasks WHERE id = ?",
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
                "created_at": row["created_at"],
            }
        return None


def list_tasks(active_only: bool = False, db_path: str | None = None) -> list[dict]:
    """List all tasks, optionally filtering to active only."""
    with get_connection(db_path) as conn:
        if active_only:
            cursor = conn.execute(
                "SELECT id, title, description, type, reward, active, created_at FROM tasks WHERE active = 1"
            )
        else:
            cursor = conn.execute(
                "SELECT id, title, description, type, reward, active, created_at FROM tasks"
            )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"],
                "type": row["type"],
                "reward": row["reward"],
                "active": bool(row["active"]),
                "created_at": row["created_at"],
            }
            for row in cursor.fetchall()
        ]


def update_task(task_id: int, title: str | None = None, description: str | None = None,
                task_type: str | None = None, reward: int | None = None,
                active: bool | None = None, db_path: str | None = None) -> bool:
    """Update a task. Returns True if the task existed and was updated."""
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
