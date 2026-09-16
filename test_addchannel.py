"""
Tests for the interactive /addchannel conversation flow.

Run:
    python -m pytest test_addchannel.py -v
    # or
    python -m unittest test_addchannel.py -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError
from telegram.ext import ConversationHandler

from config import CHANNELS, Channel
from subscription import is_locked, lock_user, unlock_user
from bot import (
    ADDCHANNEL_TITLE,
    ADDCHANNEL_USERNAME,
    _derive_slug,
    addchannel_cancel,
    addchannel_start,
    addchannel_title,
    addchannel_username,
)

# Explicit test-only admin ID — never depends on ADMINS being non-empty.
_TEST_ADMIN_ID = 88888888


# ── Test helpers ──────────────────────────────────────────────────────


def _make_update(user_id: int = 999, text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    if text is not None:
        update.message.text = text
    else:
        update.message.text = None
    return update


def _make_context(bot: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {}
    return ctx


# ── Tests: addchannel_start ───────────────────────────────────────────


class TestAddchannelStart(unittest.IsolatedAsyncioTestCase):
    """Tests for the addchannel_start entry point."""

    def setUp(self) -> None:
        unlock_user(999)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_admin_gets_username_prompt(self, _mock: MagicMock) -> None:
        """Admin sending /addchannel gets prompted for username."""
        update = _make_update(user_id=_TEST_ADMIN_ID)
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Username", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_subscribed_rejected(self, _mock: MagicMock) -> None:
        """Subscribed non-admin user is rejected with admin-only message."""
        # CHANNELS is empty → subscription check passes
        update = _make_update(user_id=999)
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_unsubscribed_gets_lock(self, _mock: MagicMock) -> None:
        """Unsubscribed non-admin user gets lock + missing channels message."""
        ch = Channel(
            slug="ch1", channel_id=-100111,
            username="ch1user", title="Ch1", required=True,
        )
        CHANNELS["ch1"] = ch

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="left"),
        )
        update = _make_update(user_id=999)
        ctx = _make_context(bot)

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertTrue(is_locked(999))
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("غير مشترك", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_start_cleans_previous_state(self, _mock: MagicMock) -> None:
        """Starting a new flow clears leftover user_data from a prior run."""
        update = _make_update(user_id=_TEST_ADMIN_ID)
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -999
        ctx.user_data["addchannel_username"] = "stale"

        await addchannel_start(update, ctx)

        self.assertNotIn("addchannel_channel_id", ctx.user_data)
        self.assertNotIn("addchannel_username", ctx.user_data)


# ── Tests: addchannel_username ────────────────────────────────────────


class TestAddchannelUsername(unittest.IsolatedAsyncioTestCase):
    """Tests for the addchannel_username handler."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_valid_at_username_accepted(self, _mock: MagicMock) -> None:
        """@username format is accepted and validated."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@testchannel")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100999)
        self.assertEqual(ctx.user_data["addchannel_username"], "testchannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_valid_tme_link_accepted(self, _mock: MagicMock) -> None:
        """t.me URL format is accepted and validated."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100888
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(
            user_id=_TEST_ADMIN_ID,
            text="https://t.me/mychannel",
        )
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100888)
        self.assertEqual(ctx.user_data["addchannel_username"], "mychannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_invalid_username_rejected(self, _mock: MagicMock) -> None:
        """Invalid username format is rejected."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="bad")
        ctx = _make_context()

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة غير صحيحة", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_channel_rejected(self, _mock: MagicMock) -> None:
        """Non-channel target is rejected."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100555
        mock_chat.type = "group"
        bot.get_chat = AsyncMock(return_value=mock_chat)

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@mygroup")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("ليست قناة", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_bot_not_admin_rejected(self, _mock: MagicMock) -> None:
        """Bot not admin in channel is rejected."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100444
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="member"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@notmychannel")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("ليس مشرفًا", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_duplicate_channel_id_rejected(self, _mock: MagicMock) -> None:
        """Duplicate channel_id is rejected safely."""
        CHANNELS["existing"] = Channel(
            slug="existing", channel_id=-100999,
            username="existing_ch", title="Existing", required=True,
        )

        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@existing_ch")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("موجودة مسبقًا", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_get_chat_error_rejected(self, _mock: MagicMock) -> None:
        """Telegram API error during get_chat is handled."""
        bot = MagicMock()
        bot.get_chat = AsyncMock(side_effect=TelegramError("Not found"))

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@badchannel")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تعذر الوصول", reply)


# ── Tests: addchannel_title ───────────────────────────────────────────


class TestAddchannelTitle(unittest.IsolatedAsyncioTestCase):
    """Tests for the addchannel_title handler."""

    def setUp(self) -> None:
        CHANNELS.clear()
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        import db
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(db.DB_PATH)

    def tearDown(self) -> None:
        import db
        db.DB_PATH = self.original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    async def test_valid_title_creates_channel(self) -> None:
        """Valid channel + title creates CHANNELS entry and SQLite row."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="My Channel")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100777
        ctx.user_data["addchannel_username"] = "testchannel"

        result = await addchannel_title(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertIn("testchannel", CHANNELS)
        ch = CHANNELS["testchannel"]
        self.assertEqual(ch.channel_id, -100777)
        self.assertEqual(ch.title, "My Channel")
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تمت إضافة القناة", reply)

    async def test_empty_title_rejected(self) -> None:
        """Empty / whitespace-only title is rejected."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="   ")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100777
        ctx.user_data["addchannel_username"] = "testchannel"

        result = await addchannel_title(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("لا يمكن أن يكون فارغًا", reply)

    async def test_channel_persists_in_sqlite(self) -> None:
        """Created channel persists to SQLite and survives reload."""
        import db

        update = _make_update(user_id=_TEST_ADMIN_ID, text="Persist Test")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100666
        ctx.user_data["addchannel_username"] = "persistch"

        await addchannel_title(update, ctx)

        retrieved = db.get_channel_from_db("persistch", self.test_db_path)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.channel_id, -100666)
        self.assertEqual(retrieved.title, "Persist Test")


# ── Tests: addchannel_cancel ──────────────────────────────────────────


class TestAddchannelCancel(unittest.IsolatedAsyncioTestCase):
    """Tests for the addchannel_cancel handler."""

    def setUp(self) -> None:
        CHANNELS.clear()

    async def test_cancel_exits_without_saving(self) -> None:
        """/cancel exits without changing database or CHANNELS."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/cancel")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100555
        ctx.user_data["addchannel_username"] = "cancelpending"

        result = await addchannel_cancel(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertNotIn("cancelpending", CHANNELS)
        self.assertNotIn("addchannel_channel_id", ctx.user_data)
        self.assertNotIn("addchannel_username", ctx.user_data)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تم الإلغاء", reply)


# ── Tests: _derive_slug ───────────────────────────────────────────────


class TestDeriveSlug(unittest.TestCase):
    """Tests for the _derive_slug helper."""

    def test_at_username(self) -> None:
        self.assertEqual(_derive_slug("@Crypto1583"), "crypto1583")

    def test_bare_username(self) -> None:
        self.assertEqual(_derive_slug("My_Channel"), "my_channel")

    def test_fallback_empty(self) -> None:
        self.assertEqual(_derive_slug("!!!"), "channel")


# ── Tests: duplicate slug safety ──────────────────────────────────────


class TestAddchannelDuplicateSlug(unittest.IsolatedAsyncioTestCase):
    """Test that duplicate slugs are handled safely."""

    def setUp(self) -> None:
        CHANNELS.clear()
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        import db
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(db.DB_PATH)

    def tearDown(self) -> None:
        import db
        db.DB_PATH = self.original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    async def test_duplicate_slug_gets_suffixed(self) -> None:
        """When the derived slug already exists, a suffix is appended."""
        CHANNELS["testchannel"] = Channel(
            slug="testchannel", channel_id=-100111,
            username="testchannel", title="Original", required=True,
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="New Channel")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100222
        ctx.user_data["addchannel_username"] = "testchannel"

        result = await addchannel_title(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        # Original still present
        self.assertIn("testchannel", CHANNELS)
        self.assertEqual(CHANNELS["testchannel"].channel_id, -100111)
        # New one has suffixed slug
        suffixed = "testchannel_100222"
        self.assertIn(suffixed, CHANNELS)
        self.assertEqual(CHANNELS[suffixed].channel_id, -100222)


if __name__ == "__main__":
    unittest.main()
