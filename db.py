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
