"""
Task-Coin Channel Management Configuration
-------------------------------------------
Foundation for mandatory subscription channels.
- ADMINS: list of Telegram user IDs with admin access.
- CHANNELS: dict of required channels keyed by a stable slug.
  Each entry stores the data needed for future membership verification.

Usage:
    from config import ADMINS, CHANNELS, is_admin

    # Check if a user is admin
    if is_admin(user_id):
        ...
"""

import logging
import os
from typing import Dict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Admin Configuration ──────────────────────────────────────────────
# List of Telegram user IDs that have admin privileges.
# Add more IDs as needed; the bot checks `user_id in ADMINS`.
ADMINS: list[int] = [
    6175354851,
]


def is_admin(user_id: int) -> bool:
    """Return True when *user_id* is an authorized administrator.

    THE single centralized authorization decision (MT-ADMIN-37 keeps
    this exact API — there is no second ``is_admin`` anywhere):

    1. the configured bootstrap list ``ADMINS`` is honored FIRST, so
       a configured administrator can never be locked out by a
       missing/absent database row; then
    2. the persistent ``admin_users`` store (``db.is_admin_user``) —
       the authoritative registry the Admin Control Center's
       ``admins`` module manages (list/add/remove).

    The store lookup is imported LAZILY inside the function because
    ``db`` imports ``config`` at module scope (this avoids an import
    cycle), and it fails CLOSED: any store error answers False for
    non-bootstrap ids and is logged without exposing anything.
    """
    if user_id in ADMINS:
        return True
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or user_id <= 0
    ):
        return False
    try:
        import db  # lazy: db imports config at module import time
        return db.is_admin_user(user_id)
    except Exception:
        logger.debug("Admin store lookup failed", exc_info=True)
        return False


# ── Channel Data Model ───────────────────────────────────────────────
# Each channel is stored in the CHANNELS dict under a stable slug (key).
# Fields:
#   slug       – unique string identifier (e.g. "main_channel")
#   channel_id – Telegram channel ID (numeric, can be negative for groups)
#   username   – @username of the channel (for links / deep links)
#   title      – human-readable display name
#   required   – whether the user MUST be a member (always True here,
#                but kept for future flexibility per-channel)


class Channel:
    """Represents a mandatory subscription channel."""

    __slots__ = ("slug", "channel_id", "username", "title", "required", "chat_type")

    def __init__(
        self,
        slug: str,
        channel_id: int,
        username: str,
        title: str,
        required: bool = True,
        chat_type: str = "channel",
    ) -> None:
        self.slug = slug
        self.channel_id = channel_id
        self.username = username
        self.title = title
        self.required = required
        self.chat_type = chat_type

    def __repr__(self) -> str:
        return f"Channel(slug={self.slug!r}, channel_id={self.channel_id}, username={self.username!r}, chat_type={self.chat_type!r})"


# ── Dynamic Channel Storage ──────────────────────────────────────────
# Add / remove / edit channels by modifying this dict.
# Keys are stable slugs; values are Channel instances.
CHANNELS: Dict[str, Channel] = {
    # Example (uncomment and fill in real values):
    # "main_channel": Channel(
    #     slug="main_channel",
    #     channel_id=-1001234567890,
    #     username="your_channel",
    #     title="قناة Task-Coin الرسمية",
    #     required=True,
    # ),
}


def get_channel(slug: str) -> Channel | None:
    """Look up a channel by its slug. Returns None if not found."""
    return CHANNELS.get(slug)


def get_required_channels() -> list[Channel]:
    """Return a list of all channels marked as required."""
    return [ch for ch in CHANNELS.values() if ch.required]


# ── Mini App URL Configuration ───────────────────────────────────────

def get_mini_app_url() -> str:
    """Validate and return the MINI_APP_URL environment variable.

    Requirements:
        - Must be present.
        - Must be a valid absolute HTTPS URL.
        - Non-HTTPS URLs are rejected.
        - No silent fallback to localhost.

    Raises:
        RuntimeError: If the URL is missing, malformed, or not HTTPS.
    """
    raw = os.environ.get("MINI_APP_URL", "").strip()

    if not raw:
        raise RuntimeError(
            "MINI_APP_URL environment variable is not set. "
            "It must be a valid HTTPS URL (e.g. https://example.com)."
        )

    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"MINI_APP_URL is malformed: {raw!r} — {exc}"
        ) from exc

    if parsed.scheme != "https":
        raise RuntimeError(
            f"MINI_APP_URL must use HTTPS. Got: {parsed.scheme!r} from {raw!r}"
        )

    if not parsed.netloc:
        raise RuntimeError(
            f"MINI_APP_URL has no hostname: {raw!r}"
        )

    return raw
