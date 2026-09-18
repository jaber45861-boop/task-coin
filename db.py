"""
TaskCoin Database Module
SQLite-based persistence for user and referral tracking.
"""

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("TASKCOIN_DB_PATH", "task_coin.db")


@contextmanager
def get_connection():
    """Get a SQLite connection with proper settings."""
    conn = sqlite3.connect(DB_PATH)
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


def init_db():
    """Initialize database schema."""
    with get_connection() as conn:
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
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, referred_by) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, referred_by)
        )
    
    return True


def get_user(user_id: int) -> dict | None:
    """Get user by ID."""
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
                "created_at": row["created_at"]
            }
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
