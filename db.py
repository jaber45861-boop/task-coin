"""
SQLite persistence for required channels only.

This module provides a thin layer over SQLite to store and load
the required-channel configuration so it survives bot restarts.
"""

import sqlite3
import logging
from typing import Optional

from config import Channel, CHANNELS

logger = logging.getLogger(__name__)

DB_PATH = "task_coin.db"


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for better concurrency."""
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Initialize the required_channels table if it doesn't exist."""
    if db_path is None:
        db_path = DB_PATH
    conn = get_connection(db_path)
    try:
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
        conn.commit()
        logger.info("Database initialized: %s", db_path)
    finally:
        conn.close()


def load_channels(db_path: str | None = None) -> None:
    """Load all required channels from SQLite into the in-memory CHANNELS dict."""
    if db_path is None:
        db_path = DB_PATH
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title, required, chat_type FROM required_channels"
        )
        rows = cursor.fetchall()
        
        CHANNELS.clear()
        for slug, channel_id, username, title, required, chat_type in rows:
            CHANNELS[slug] = Channel(
                slug=slug,
                channel_id=channel_id,
                username=username,
                title=title,
                required=bool(required),
                chat_type=chat_type if chat_type else "channel",
            )
        
        logger.info("Loaded %d channels from database", len(rows))
    finally:
        conn.close()


def save_channel(channel: Channel, db_path: str | None = None) -> None:
    """Persist a channel to the SQLite database."""
    if db_path is None:
        db_path = DB_PATH
    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO required_channels 
               (slug, channel_id, username, title, required, chat_type)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel.slug, channel.channel_id, channel.username, 
             channel.title, int(channel.required), channel.chat_type)
        )
        conn.commit()
        logger.info("Channel saved to DB: %s", channel.slug)
    finally:
        conn.close()


def delete_channel(slug: str, db_path: str | None = None) -> None:
    """Remove a channel from the SQLite database."""
    if db_path is None:
        db_path = DB_PATH
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM required_channels WHERE slug = ?", (slug,))
        conn.commit()
        logger.info("Channel deleted from DB: %s", slug)
    finally:
        conn.close()


def get_channel_from_db(slug: str, db_path: str | None = None) -> Optional[Channel]:
    """Retrieve a single channel from the database."""
    if db_path is None:
        db_path = DB_PATH
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT slug, channel_id, username, title, required, chat_type FROM required_channels WHERE slug = ?",
            (slug,)
        )
        row = cursor.fetchone()
        if row:
            return Channel(
                slug=row[0], channel_id=row[1], username=row[2],
                title=row[3], required=bool(row[4]),
                chat_type=row[5] if row[5] else "channel",
            )
        return None
    finally:
        conn.close()
