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

from typing import Dict

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

    __slots__ = ("slug", "channel_id", "username", "title", "required")

    def __init__(
        self,
        slug: str,
        channel_id: int,
        username: str,
        title: str,
        required: bool = True,
    ) -> None:
        self.slug = slug
        self.channel_id = channel_id
        self.username = username
        self.title = title
        self.required = required

    def __repr__(self) -> str:
        return f"Channel(slug={self.slug!r}, channel_id={self.channel_id}, username={self.username!r})"


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
