"""
Subscription Access Gate
------------------------
Reusable helper for mandatory-subscription enforcement.

Core function:
    check_subscription_access(bot, user_id) -> (is_subscribed, missing_channels)

The helper always re-queries the Telegram API for each required channel,
so it is never stale after a restart.  Telegram API errors on individual
channels are treated as "not verified" (fail-closed) without raising.

Lock state (in-memory dict keyed by user_id) provides immediate blocking
between the time a chat_member departure is detected and the next API
re-verification.  The lock is purely an optimization; access decisions
are always ultimately made by live Telegram queries.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from telegram.error import TelegramError

from config import Channel, get_required_channels, is_admin

if TYPE_CHECKING:
    from telegram import Bot


# ── In-memory lock state ─────────────────────────────────────────────
# Maps user_id -> timestamp when they were locked.
# This is NOT the source of truth; live API checks are.
_locked_users: dict[int, float] = {}


def is_locked(user_id: int) -> bool:
    """Return True if the user is known to be missing required channels."""
    return user_id in _locked_users


def lock_user(user_id: int) -> None:
    """Mark a user as locked (missing required channels)."""
    _locked_users[user_id] = time.time()


def unlock_user(user_id: int) -> None:
    """Clear the lock for a user."""
    _locked_users.pop(user_id, None)


# ── Subscription verification ─────────────────────────────────────────

# Statuses that count as "subscribed"
_MEMBER_STATUSES = frozenset({"member", "administrator", "creator"})


def _is_chat_member(status: str) -> bool:
    """Return True if the Telegram ChatMember status means 'subscribed'."""
    return status in _MEMBER_STATUSES


async def check_subscription_access(
    bot: "Bot", user_id: int
) -> tuple[bool, list[Channel]]:
    """Check whether *user_id* is subscribed to every required channel.

    Returns:
        (True, [])          – user is subscribed to all required channels
        (False, [Channel…]) – list of channels the user is NOT subscribed to

    Telegram API errors for individual channels are caught and that channel
    is counted as **not** verified (fail-closed).
    """
    required = get_required_channels()
    if not required:
        return True, []

    missing: list[Channel] = []
    for ch in required:
        try:
            member = await bot.get_chat_member(ch.channel_id, user_id)
            if not _is_chat_member(member.status):
                missing.append(ch)
        except TelegramError:
            missing.append(ch)

    return (len(missing) == 0, missing)
