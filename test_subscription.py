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

# Explicit test-only admin ID — never depends on ADMINS being non-empty.
_TEST_ADMIN_ID = 88888888
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
        _setup_channels([_CHANNEL_A])

        async def fake_get_chat_member(channel_id: int, user_id: int):
            return _make_chat_member("left")

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)

        ok, missing = await check_subscription_access(bot, _TEST_ADMIN_ID)

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
    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_admin_bypasses_subscription_check(self, _mock_is_admin: MagicMock) -> None:
        from bot import add_channel

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel bad_format_only_one_part"
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


class TestAddChannelUsernameInput(unittest.IsolatedAsyncioTestCase):
    """Tests for the new /addchannel username-based input format."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(999)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_at_username(self, _mock: MagicMock) -> None:
        """slug|@channelusername|title resolves and stores correctly."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"

        mock_bot_member = _make_chat_member("administrator")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel main|@testchannel|Test Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        # Channel should be stored
        self.assertIn("main", CHANNELS)
        ch = CHANNELS["main"]
        self.assertEqual(ch.channel_id, -100999)
        self.assertEqual(ch.username, "testchannel")
        self.assertEqual(ch.title, "Test Title")

        # Success message
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تمت إضافة القناة", reply_text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_tme_link(self, _mock: MagicMock) -> None:
        """slug|https://t.me/channelusername|title resolves correctly."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100888
        mock_chat.type = "channel"

        mock_bot_member = _make_chat_member("administrator")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel mych|https://t.me/mychannel|My Channel"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        self.assertIn("mych", CHANNELS)
        ch = CHANNELS["mych"]
        self.assertEqual(ch.channel_id, -100888)
        self.assertEqual(ch.username, "mychannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_bare_username(self, _mock: MagicMock) -> None:
        """slug|username|title (bare, no @) also works."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100777
        mock_chat.type = "channel"

        mock_bot_member = _make_chat_member("administrator")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel ch2|barechannel|Bare Channel"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        self.assertIn("ch2", CHANNELS)
        self.assertEqual(CHANNELS["ch2"].username, "barechannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_wrong_part_count(self, _mock: MagicMock) -> None:
        """Wrong number of parts shows help text."""
        from bot import add_channel

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel too|many|parts|here"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة خاطئة", reply_text)
        self.assertEqual(len(CHANNELS), 0)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_invalid_username(self, _mock: MagicMock) -> None:
        """Invalid channel reference shows error."""
        from bot import add_channel

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel main|bad|Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة غير صحيحة", reply_text)
        self.assertEqual(len(CHANNELS), 0)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_not_a_channel(self, _mock: MagicMock) -> None:
        """Resolving a group instead of a channel shows error."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100555
        mock_chat.type = "group"  # not a channel!

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel grp|@mygroup|Group Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("ليست قناة", reply_text)
        self.assertEqual(len(CHANNELS), 0)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_bot_not_admin(self, _mock: MagicMock) -> None:
        """Bot not being admin in the channel shows error."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100444
        mock_chat.type = "channel"

        mock_bot_member = _make_chat_member("member")  # not admin!

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel ch3|@notmychannel|Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("ليس مشرفًا", reply_text)
        self.assertEqual(len(CHANNELS), 0)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_duplicate_channel_id(self, _mock: MagicMock) -> None:
        """Same channel added twice via different slug is rejected."""
        from bot import add_channel

        # Pre-populate with a channel
        _setup_channels([Channel(
            slug="existing",
            channel_id=-100999,
            username="existing_ch",
            title="Existing",
            required=True,
        )])

        mock_chat = MagicMock()
        mock_chat.id = -100999  # same ID!
        mock_chat.type = "channel"

        mock_bot_member = _make_chat_member("administrator")

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel dup|@existing_ch|Duplicate"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("موجودة مسبقًا", reply_text)
        self.assertEqual(len(CHANNELS), 1)  # still only the original

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_get_chat_error(self, _mock: MagicMock) -> None:
        """Telegram API error during get_chat shows error."""
        from bot import add_channel

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel ch4|@badchannel|Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(side_effect=TelegramError("Not found"))
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تعذر الوصول", reply_text)
        self.assertEqual(len(CHANNELS), 0)


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
        """Correct answer + all channels subscribed → success message."""
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
        # User should be unlocked
        self.assertFalse(is_locked(999))

    async def test_start_missing_channel_shows_lock(self) -> None:
        """Correct answer + missing channel → lock + missing message."""
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
        """Correct answer + no required channels → success behavior."""
        CHANNELS.clear()
        from bot import check_answer

        update = _make_update(user_id=999, text="42")
        ctx = _make_context()
        ctx.user_data["anti_bot_answer"] = 42

        result = await check_answer(update, ctx)

        self.assertEqual(result, -1)
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تحقق ناجح", reply_text)

    async def test_start_triggers_subscription_check(self) -> None:
        """Prove that a correct Anti-Bot answer immediately triggers
        the required-channel check (get_chat_member is called)."""
        _setup_channels([_CHANNEL_A])
        from bot import check_answer

        update = _make_update(user_id=999, text="10")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("member"))
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 10

        await check_answer(update, ctx)

        # get_chat_member MUST have been called → subscription was checked
        bot.get_chat_member.assert_called_once()
        call_args = bot.get_chat_member.call_args
        self.assertEqual(call_args[0][0], _CHANNEL_A.channel_id)
        self.assertEqual(call_args[0][1], 999)

    async def test_start_missing_channel_shows_buttons(self) -> None:
        """Correct answer + missing channel → InlineKeyboardMarkup
        with channel links and verify button appear immediately."""
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        from bot import check_answer
        from telegram import InlineKeyboardMarkup

        async def fake_get_chat_member(channel_id: int, user_id: int):
            if channel_id == _CHANNEL_A.channel_id:
                return _make_chat_member("member")
            return _make_chat_member("left")

        update = _make_update(user_id=999, text="42")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=fake_get_chat_member)
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 42

        await check_answer(update, ctx)

        # reply_text was called with text + reply_markup
        call_kwargs = update.message.reply_text.call_args
        markup = call_kwargs[1].get("reply_markup") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        )
        self.assertIsInstance(markup, InlineKeyboardMarkup)

        # Flatten all buttons
        flat_buttons = [btn for row in markup.inline_keyboard for btn in row]
        # Should have one button per missing channel + one verify button
        self.assertEqual(len(flat_buttons), 2)  # 1 missing channel + 1 verify

        # Channel button URL should link to the missing channel
        channel_btn = flat_buttons[0]
        self.assertIn(_CHANNEL_B.username, channel_btn.url)

        # Verify button should have correct callback_data
        verify_btn = flat_buttons[1]
        self.assertEqual(verify_btn.callback_data, "verify_subscription")
        self.assertIn("تحقق", verify_btn.text)

    async def test_start_wrong_answer_no_subscription_check(self) -> None:
        """Wrong answer → no subscription check, conversation ends."""
        _setup_channels([_CHANNEL_A])
        from bot import check_answer

        update = _make_update(user_id=999, text="999")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock()
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 42

        result = await check_answer(update, ctx)

        self.assertEqual(result, -1)
        # get_chat_member should NOT have been called
        bot.get_chat_member.assert_not_called()
        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("إجابة غير صحيحة", reply_text)

    async def test_correct_answer_produces_exactly_one_reply(self) -> None:
        """Regression: a correct Anti-Bot answer with missing channels
        must produce EXACTLY ONE reply_text call (no duplicate messages)."""
        _setup_channels([_CHANNEL_A])
        from bot import check_answer

        update = _make_update(user_id=999, text="42")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("left"))
        ctx = _make_context(bot)
        ctx.user_data["anti_bot_answer"] = 42

        await check_answer(update, ctx)

        # Exactly ONE reply — no duplicates
        self.assertEqual(update.message.reply_text.call_count, 1)

    async def test_normal_text_outside_anti_bot_goes_through_gate(self) -> None:
        """Normal text messages (no active conversation) must still
        be processed by subscription_message_gate."""
        _setup_channels([_CHANNEL_A])
        from bot import subscription_message_gate

        update = _make_update(user_id=999, text="hello")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=_make_chat_member("left"))
        ctx = _make_context(bot)
        # No anti_bot_answer — user is NOT in a conversation

        await subscription_message_gate(update, ctx)

        # Gate should have checked subscription and sent a message
        bot.get_chat_member.assert_called_once()
        update.message.reply_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
