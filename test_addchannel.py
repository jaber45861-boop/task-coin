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
    _normalize_channel_ref,
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

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_supergroup_accepted(self, _mock: MagicMock) -> None:
        """Supergroup type is accepted like a channel."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100777
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@mygroup")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100777)
        self.assertEqual(ctx.user_data["addchannel_username"], "mygroup")
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "supergroup")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_supergroup_bot_not_admin_rejected(self, _mock: MagicMock) -> None:
        """Supergroup where bot is not admin is rejected."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100666
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="member"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@notmygroup")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("ليس مشرفًا", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_supergroup_stores_chat_type(self, _mock: MagicMock) -> None:
        """Supergroup stores chat_type='supergroup' for later use."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100555
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@mygroup")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "supergroup")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_channel_stores_chat_type(self, _mock: MagicMock) -> None:
        """Channel stores chat_type='channel' for later use."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100444
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(user_id=_TEST_ADMIN_ID, text="@mychannel")
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "channel")


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

    async def test_supergroup_chat_type_persisted(self) -> None:
        """Supergroup chat_type is persisted to SQLite."""
        import db

        update = _make_update(user_id=_TEST_ADMIN_ID, text="My Group")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100555
        ctx.user_data["addchannel_username"] = "mygroup"
        ctx.user_data["addchannel_chat_type"] = "supergroup"

        await addchannel_title(update, ctx)

        self.assertIn("mygroup", CHANNELS)
        ch = CHANNELS["mygroup"]
        self.assertEqual(ch.chat_type, "supergroup")

        # Verify persisted to SQLite
        retrieved = db.get_channel_from_db("mygroup", self.test_db_path)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.chat_type, "supergroup")

    async def test_channel_chat_type_default(self) -> None:
        """When chat_type is not set, defaults to 'channel'."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="Default Type")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -100333
        ctx.user_data["addchannel_username"] = "defaultch"
        # No addchannel_chat_type set — should default to 'channel'

        await addchannel_title(update, ctx)

        self.assertIn("defaultch", CHANNELS)
        ch = CHANNELS["defaultch"]
        self.assertEqual(ch.chat_type, "channel")


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



# ── Tests: _normalize_channel_ref (parser) ───────────────────────────


class TestNormalizeChannelRef(unittest.TestCase):
    """Direct unit tests for the _normalize_channel_ref parser.

    Covers all accepted input formats: @username, t.me/, www.t.me/,
    https://, http://, and bare username.
    """

    def test_at_username(self) -> None:
        result = _normalize_channel_ref("@Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_bare_username(self) -> None:
        result = _normalize_channel_ref("Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_https_tme(self) -> None:
        result = _normalize_channel_ref("https://t.me/Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_http_tme(self) -> None:
        result = _normalize_channel_ref("http://t.me/Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_www_tme(self) -> None:
        result = _normalize_channel_ref("www.t.me/Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_bare_tme(self) -> None:
        """Bare t.me/ link without protocol must be accepted."""
        result = _normalize_channel_ref("t.me/Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_https_www_tme(self) -> None:
        result = _normalize_channel_ref("https://www.t.me/Crypto1583")
        self.assertEqual(result, "Crypto1583")

    def test_short_username_rejected(self) -> None:
        """Usernames shorter than 5 characters are rejected."""
        result = _normalize_channel_ref("@abc")
        self.assertIsNone(result)

    def test_invalid_chars_rejected(self) -> None:
        result = _normalize_channel_ref("@bad username")
        self.assertIsNone(result)

    def test_none_returns_none(self) -> None:
        result = _normalize_channel_ref("")
        self.assertIsNone(result)

    def test_whitespace_stripped(self) -> None:
        result = _normalize_channel_ref("  @Crypto1583  ")
        self.assertEqual(result, "Crypto1583")


# ── Tests: addchannel_username with bare t.me/ links ──────────────────


class TestAddchannelUsernameBareTmeLink(unittest.IsolatedAsyncioTestCase):
    """Tests that bare t.me/ links (no protocol) are accepted
    in the interactive addchannel_username handler.

    These are the cases that previously failed.
    """

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_bare_tme_channel_accepted(self, _mock: MagicMock) -> None:
        """Bare t.me/channelusername for a channel is accepted."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(
            user_id=_TEST_ADMIN_ID, text="t.me/testchannel",
        )
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100999)
        self.assertEqual(ctx.user_data["addchannel_username"], "testchannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_bare_tme_supergroup_accepted(self, _mock: MagicMock) -> None:
        """Bare t.me/groupusername for a supergroup is accepted."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100777
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(
            user_id=_TEST_ADMIN_ID, text="t.me/mygroup",
        )
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100777)
        self.assertEqual(ctx.user_data["addchannel_username"], "mygroup")
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "supergroup")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_www_tme_channel_accepted(self, _mock: MagicMock) -> None:
        """www.t.me/channelusername without protocol is accepted."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100888
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        update = _make_update(
            user_id=_TEST_ADMIN_ID, text="www.t.me/testchannel",
        )
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100888)
        self.assertEqual(ctx.user_data["addchannel_username"], "testchannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_bare_tme_supergroup_bot_not_admin_rejected(
        self, _mock: MagicMock
    ) -> None:
        """Bare t.me supergroup where bot is not admin is rejected."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100666
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="member"),
        )

        update = _make_update(
            user_id=_TEST_ADMIN_ID, text="t.me/notmygroup",
        )
        ctx = _make_context(bot)

        result = await addchannel_username(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("ليس مشرفًا", reply)


# ── Tests: legacy add_channel with bare t.me/ links ───────────────────


class TestLegacyAddChannelBareTmeLink(unittest.IsolatedAsyncioTestCase):
    """Tests that the legacy /addchannel slug|ref|title command
    also accepts bare t.me/ links via _normalize_channel_ref.
    """

    def setUp(self) -> None:
        CHANNELS.clear()
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        import db as _db
        self.original_db_path = _db.DB_PATH
        _db.DB_PATH = self.test_db_path
        _db.init_db(_db.DB_PATH)

    def tearDown(self) -> None:
        import db as _db
        _db.DB_PATH = self.original_db_path
        CHANNELS.clear()
        if os.path.exists(self.test_db_path):
            os.unlink(self.test_db_path)
        for suffix in ("-wal", "-shm"):
            p = self.test_db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_legacy_bare_tme_link(self, _mock: MagicMock) -> None:
        """slug|t.me/username|title works in the legacy command."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"

        mock_bot_member = MagicMock()
        mock_bot_member.status = "administrator"

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel main|t.me/testchannel|Test Title"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        self.assertIn("main", CHANNELS)
        ch = CHANNELS["main"]
        self.assertEqual(ch.channel_id, -100999)
        self.assertEqual(ch.username, "testchannel")
        self.assertEqual(ch.title, "Test Title")

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تمت إضافة القناة", reply_text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_legacy_bare_tme_supergroup(self, _mock: MagicMock) -> None:
        """slug|t.me/groupname|title for a supergroup works in legacy."""
        from bot import add_channel

        mock_chat = MagicMock()
        mock_chat.id = -100777
        mock_chat.type = "supergroup"

        mock_bot_member = MagicMock()
        mock_bot_member.status = "administrator"

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = _TEST_ADMIN_ID
        update.message = MagicMock()
        update.message.text = "/addchannel grp|t.me/mygroup|My Group"
        update.message.reply_text = AsyncMock()

        bot = MagicMock()
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(return_value=mock_bot_member)
        ctx = _make_context(bot)

        await add_channel(update, ctx)

        self.assertIn("grp", CHANNELS)
        ch = CHANNELS["grp"]
        self.assertEqual(ch.channel_id, -100777)
        self.assertEqual(ch.username, "mygroup")
        self.assertEqual(ch.title, "My Group")
        self.assertEqual(ch.chat_type, "supergroup")

        reply_text = update.message.reply_text.call_args[0][0]
        self.assertIn("تمت إضافة القناة", reply_text)


if __name__ == "__main__":
    unittest.main()
