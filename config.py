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

import os
from typing import Dict
from urllib.parse import urlparse

# ── Admin Configuration ──────────────────────────────────────────────
# List of Telegram user IDs that have admin privileges.
# Add more IDs as needed; the bot checks `user_id in ADMINS`.
ADMINS: list[int] = [
    6175354851,
]


def is_admin(user_id: int) -> bool:
    """Return True if user_id is in the ADMINS list."""
    return user_id in ADMINS


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
