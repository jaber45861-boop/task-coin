"""
Comprehensive tests for the Force Subscription lock behaviour.

Run:
    python -m pytest test_subscription.py -v
    # or
    python -m unittest test_subscription.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError

from config import ADMINS, CHANNELS, Channel, is_admin
from subscription import (
    check_subscription_access,
    is_locked,
    lock_user,
    unlock_user,
)


# ── Test helpers ──────────────────────────────────────────────────────

def _make_channel(slug: str, channel_id: int, title: str = "Test") -> Channel:
    return Channel(
        slug=slug,
        channel_id=channel_id,
        username=f"{slug}_user",
        title=title,
        required=True,
    )


def _make_chat_member(status: str) -> MagicMock:
    member = MagicMock()
    member.status = status
    return member


def _make_update(
    user_id: int = 999,
    text: str | None = None,
    is_admin: bool = False,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    if text is not None:
        update.message = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
    else:
        update.message = None
    update.callback_query = MagicMock()
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _make_context(
    bot: MagicMock | None = None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {}
    return ctx


# ── Helper: set up channels ──────────────────────────────────────────

_CHANNEL_A = _make_channel("ch_a", -100111, "Channel A")
_CHANNEL_B = _make_channel("ch_b", -100222, "Channel B")
_CHANNEL_C = _make_channel("ch_c", -100333, "Channel C")


def _setup_channels(channels: list[Channel]) -> None:
    """Populate CHANNELS dict for testing."""
    CHANNELS.clear()
    for ch in channels:
        CHANNELS[ch.slug] = ch


# ── Tests ─────────────────────────────────────────────────────────────


class TestCheckSubscriptionAccess(unittest.IsolatedAsyncioTestCase):
    """Tests for the core check_subscription_access() helper."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()

    # 1. User subscribed to all required channels → access allowed
    async def test_subscribed_to_all(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("member"))

        ok, missing = await check_subscription_access(bot, 999)

        self.assertTrue(ok)
        self.assertEqual(missing, [])

    # 2. User missing one required channel → access blocked
    async def test_missing_one_channel(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                return _make_chat_member("left")
            return _make_chat_member("member")

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)

        ok, missing = await check_subscription_access(bot, 999)

        self.assertFalse(ok)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].channel_id, _CHANNEL_A.channel_id)

    # 3. User missing multiple required channels → access blocked
    async def test_missing_multiple_channels(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B, _CHANNEL_C])

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                return _make_chat_member("left")
            if channel_id == _CHANNEL_B.channel_id:
                return _make_chat_member("kicked")
            return _make_chat_member("member")

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)

        ok, missing = await check_subscription_access(bot, 999)

        self.assertFalse(ok)
        self.assertEqual(len(missing), 2)
        ids = {ch.channel_id for ch in missing}
        self.assertIn(_CHANNEL_A.channel_id, ids)
        self.assertIn(_CHANNEL_B.channel_id, ids)

    # 7. Telegram API verification failure → treated as missing, no crash
    async def test_api_error_treated_as_missing(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                raise TelegramError("Forbidden: bot not admin")
            return _make_chat_member("member")

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)

        ok, missing = await check_subscription_access(bot, 999)

        self.assertFalse(ok)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].channel_id, _CHANNEL_A.channel_id)

    # No required channels → always allowed
    async def test_no_required_channels(self) -> None:
        CHANNELS.clear()
        bot = MagicMock()
        ok, missing = await check_subscription_access(bot, 999)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    # Admin user gets same treatment (admin bypass is in handlers, not helper)
    async def test_admin_user_checked_normally_by_helper(self) -> None:
        admin_id = ADMINS[0]
        _setup_channels([_CHANNEL_A])

        async def fake_get_chat_member(channel_id: int, user_id: int):
            return _make_chat_member("left")

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)

        ok, missing = await check_subscription_access(bot, admin_id)

        # The helper does NOT bypass admins; handlers do that.
        self.assertFalse(ok)
        self.assertEqual(len(missing), 1)


class TestLockState(unittest.TestCase):
    """Tests for the in-memory lock state helpers."""

    def setUp(self) -> None:
        unlock_user(999)

    def test_lock_and_unlock(self) -> None:
        self.assertFalse(is_locked(999))
        lock_user(999)
        self.assertTrue(is_locked(999))
        unlock_user(999)
        self.assertFalse(is_locked(999))

    def test_lock_user_idempotent(self) -> None:
        lock_user(999)
        lock_user(999)
        self.assertTrue(is_locked(999))
        unlock_user(999)
        self.assertFalse(is_locked(999))

    def test_unlock_nonexistent_is_safe(self) -> None:
        unlock_user(99999)  # should not raise


class TestChatMemberHandler(unittest.IsolatedAsyncioTestCase):
    """Tests for the chat_member update handler in bot.py."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()
        _setup_channels([_CHANNEL_A, _CHANNEL_B])

    # 4. Leaving one required channel → chat_member handler detects it
    async def test_leave_channel_locks_user(self) -> None:
        from bot import on_chat_member_update

        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = _CHANNEL_A.channel_id
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "left"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = 999
        update.chat_member = chat_member_update

        ctx = _make_context()

        # Patch _REQUIRED_CHANNEL_IDS to include our channels
        with patch("bot._REQUIRED_CHANNEL_IDS", {_CHANNEL_A.channel_id, _CHANNEL_B.channel_id}):
            await on_chat_member_update(update, ctx)

        self.assertTrue(is_locked(999))

    # Kicked also locks
    async def test_kicked_channel_locks_user(self) -> None:
        from bot import on_chat_member_update

        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = _CHANNEL_B.channel_id
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "kicked"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = 999
        update.chat_member = chat_member_update

        ctx = _make_context()

        with patch("bot._REQUIRED_CHANNEL_IDS", {_CHANNEL_A.channel_id, _CHANNEL_B.channel_id}):
            await on_chat_member_update(update, ctx)

        self.assertTrue(is_locked(999))

    # Non-required channel leave → does NOT lock
    async def test_leave_non_required_no_lock(self) -> None:
        from bot import on_chat_member_update

        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = -999999  # not a required channel
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "left"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = 999
        update.chat_member = chat_member_update

        ctx = _make_context()

        with patch("bot._REQUIRED_CHANNEL_IDS", {_CHANNEL_A.channel_id}):
            await on_chat_member_update(update, ctx)

        self.assertFalse(is_locked(999))

    # Joining/remaining member → does NOT lock
    async def test_member_status_no_lock(self) -> None:
        from bot import on_chat_member_update

        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = _CHANNEL_A.channel_id
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "member"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = 999
        update.chat_member = chat_member_update

        ctx = _make_context()

        with patch("bot._REQUIRED_CHANNEL_IDS", {_CHANNEL_A.channel_id, _CHANNEL_B.channel_id}):
            await on_chat_member_update(update, ctx)

        self.assertFalse(is_locked(999))

    # Non-required channel leave does not affect lock state
    async def test_leave_non_required_does_not_lock(self) -> None:
        from bot import on_chat_member_update

        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = -999999
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "left"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = 999
        update.chat_member = chat_member_update

        ctx = _make_context()

        with patch("bot._REQUIRED_CHANNEL_IDS", {_CHANNEL_A.channel_id}):
            await on_chat_member_update(update, ctx)

        self.assertFalse(is_locked(999))


class TestVerifyCallback(unittest.IsolatedAsyncioTestCase):
    """Tests for the verify_subscription callback handler."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()

    # 6. Rejoining ALL required channels → verify callback unlocks access
    async def test_verify_all_subscribed_unlocks(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        from bot import verify_subscription

        lock_user(999)

        update = _make_update(user_id=999)
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("member"))
        ctx = _make_context(bot)

        await verify_subscription(update, ctx)

        self.assertFalse(is_locked(999))
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("مشترك", text)

    # 5. Rejoining only one of multiple required channels → remains blocked
    async def test_verify_partial_rejoin_stays_locked(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        from bot import verify_subscription

        lock_user(999)

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                return _make_chat_member("member")
            return _make_chat_member("left")

        update = _make_update(user_id=999)
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)

        await verify_subscription(update, ctx)

        self.assertTrue(is_locked(999))
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("غير مشترك", text)

    # Rejoining ALL channels when only one is required
    async def test_verify_single_channel_unlocks(self) -> None:
        _setup_channels([_CHANNEL_A])
        from bot import verify_subscription

        lock_user(999)

        update = _make_update(user_id=999)
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("member"))
        ctx = _make_context(bot)

        await verify_subscription(update, ctx)

        self.assertFalse(is_locked(999))

    # API error during verify → treated as missing
    async def test_verify_api_error_stays_locked(self) -> None:
        _setup_channels([_CHANNEL_A])
        from bot import verify_subscription

        lock_user(999)

        async def fake_get_chat_member(channel_id: int, user_id: int):
            raise TelegramError("Forbidden")

        update = _make_update(user_id=999)
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)

        await verify_subscription(update, ctx)

        self.assertTrue(is_locked(999))

    # No required channels → success message
    async def test_verify_no_channels(self) -> None:
        CHANNELS.clear()
        from bot import verify_subscription

        update = _make_update(user_id=999)
        ctx = _make_context()

        await verify_subscription(update, ctx)

        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("لا توجد قنوات", text)


class TestAdminBypass(unittest.IsolatedAsyncioTestCase):
    """Tests that admin commands remain accessible to configured admins."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()
        _setup_channels([_CHANNEL_A])

    # 8. Admin commands remain accessible to configured admins
    async def test_admin_bypasses_subscription_check(self) -> None:
        admin_id = ADMINS[0]
        from bot import add_channel

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = admin_id
        update.message = MagicMock()
        update.message.text = "/addchannel bad_format"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        # Even if subscription check returns missing, admin should bypass
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("left"))
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        # Admin should get the "bad format" message, NOT the subscription lock
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة خاطئة", reply_text)

    async def test_non_admin_blocked_by_subscription(self) -> None:
        from bot import add_channel

        async def fake_get_chat_member(channel_id: int, user_id: int):
            return _make_chat_member("left")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.message = MagicMock()
        update.message.text = "/addchannel test"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        # Should get subscription lock message, not admin-only message
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("غير مشترك", reply_text)

    async def test_non_admin_subscribed_gets_admin_only(self) -> None:
        from bot import add_channel

        async def fake_get_chat_member(channel_id: int, user_id: int):
            return _make_chat_member("member")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 999
        update.message = MagicMock()
        update.message.text = "/addchannel test"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        # Subscribed non-admin should get "admin only" message
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply_text)


class TestNoOldBusinessLogic(unittest.TestCase):
    """9. Verify no old reward/penalty business logic was introduced."""

    def test_no_reward_deduction_code(self) -> None:
        import subscription
        import bot as bot_mod

        sub_src = open(subscription.__file__).read()
        bot_src = open(bot_mod.__file__).read()
        combined = sub_src + bot_src

        forbidden_keywords = [
            "reward",
            "deduct",
            "penalty",
            "wallet",
            "balance",
            "coin",
            "activate",
            "activation",
            "referral",
            "referral_bonus",
            "sqlite",
            "balance_deduct",
            "add_balance",
        ]
        for kw in forbidden_keywords:
            self.assertNotIn(
                kw,
                combined.lower(),
                f"Found forbidden keyword '{kw}' — old business logic detected",
            )


class TestConversationFlow(unittest.IsolatedAsyncioTestCase):
    """Tests for the /start anti-bot → subscription flow."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()

    async def test_start_all_subscribed_shows_success(self) -> None:
        _setup_channels([_CHANNEL_A])
        from bot import check_answer

        update = _make_update(user_id=999, text="42")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("member"))
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 42

        result = await check_answer(update, ctx)

        # Conversation should end
        self.assertEqual(result, -1)  # ConversationHandler.END == -1
        # Should show success
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("مشترك في جميع القنوات", reply_text)

    async def test_start_missing_channel_shows_lock(self) -> None:
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        from bot import check_answer

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                return _make_chat_member("member")
            return _make_chat_member("left")

        update = _make_update(user_id=999, text="42")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 42

        result = await check_answer(update, ctx)

        self.assertEqual(result, -1)  # ConversationHandler.END == -1
        # Should show missing channels
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("غير مشترك", reply_text)
        self.assertTrue(is_locked(999))

    async def test_start_no_channels_shows_success(self) -> None:
        CHANNELS.clear()
        from bot import check_answer

        update = _make_update(user_id=999, text="42")
        ctx = _make_context()
        ctx.user_data["anti_bot_answer"] = 42

        result = await check_answer(update, ctx)

        self.assertEqual(result, -1)
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تحقق ناجح", reply_text)


if __name__ == "__main__":
    unittest.main()
