"""
Tests for the interactive /removechannel conversation flow.

Run:
    python -m pytest test_removechannel.py -v
    # or
    python -m unittest test_removechannel.py -v
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import TelegramError
from telegram.ext import ConversationHandler

from config import CHANNELS, Channel, is_admin
from subscription import is_locked, lock_user, unlock_user
from bot import (
    REMOVECHANNEL_CONFIRM,
    REMOVECHANNEL_SELECT,
    removechannel_cancel,
    removechannel_cancel_cb,
    removechannel_confirm,
    removechannel_select,
    removechannel_start,
)

_TEST_ADMIN_ID = 88888888
_NON_ADMIN_ID = 999


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


def _make_callback(
    user_id: int, data: str,
) -> MagicMock:
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_user = update.callback_query.from_user
    return update


def _make_context(bot: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {}
    return ctx


_CHANNEL_A = Channel(
    slug="ch_a", channel_id=-100111,
    username="ch_a_user", title="Channel A", required=True,
)
_CHANNEL_B = Channel(
    slug="ch_b", channel_id=-100222,
    username="ch_b_user", title="Channel B", required=True,
)


def _setup_channels(channels: list[Channel]) -> None:
    CHANNELS.clear()
    for ch in channels:
        CHANNELS[ch.slug] = ch


# ── Tests: removechannel_start ────────────────────────────────────────


class TestRemovechannelStart(unittest.IsolatedAsyncioTestCase):
    """Tests for the removechannel_start entry point."""

    def setUp(self) -> None:
        unlock_user(_NON_ADMIN_ID)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_no_channels_shows_message(self, _mock: MagicMock) -> None:
        """With no channels, the workflow ends immediately."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/removechannel")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("لا توجد قنوات", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_channels_shows_inline_buttons(self, _mock: MagicMock) -> None:
        """With channels, inline buttons are shown."""
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/removechannel")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, REMOVECHANNEL_SELECT)
        kwargs = update.message.reply_text.call_args[1]
        markup = kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        flat = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0].callback_data, "rmch:ch_a")
        self.assertEqual(flat[1].callback_data, "rmch:ch_b")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_direct_slug_deletes(self, _mock: MagicMock) -> None:
        """/removechannel <slug> performs direct deletion."""
        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = test_db.name
        test_db.close()
        import db as _db
        orig = _db.DB_PATH
        _db.DB_PATH = db_path
        _db.init_db(db_path)
        try:
            _setup_channels([_CHANNEL_A])
            _db.save_channel(_CHANNEL_A, db_path)
            update = _make_update(
                user_id=_TEST_ADMIN_ID, text="/removechannel ch_a",
            )
            ctx = _make_context()

            result = await removechannel_start(update, ctx)

            self.assertEqual(result, ConversationHandler.END)
            self.assertNotIn("ch_a", CHANNELS)
            reply = update.message.reply_text.call_args[0][0]
            self.assertIn("تم حذف القناة", reply)
            # Verify SQLite
            self.assertIsNone(_db.get_channel_from_db("ch_a", db_path))
        finally:
            _db.DB_PATH = orig
            if os.path.exists(db_path):
                os.unlink(db_path)
            for sfx in ("-wal", "-shm"):
                p = db_path + sfx
                if os.path.exists(p):
                    os.unlink(p)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_direct_slug_not_found(self, _mock: MagicMock) -> None:
        """/removechannel nonexistent shows error."""
        update = _make_update(
            user_id=_TEST_ADMIN_ID, text="/removechannel nosuch",
        )
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("غير موجودة", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_subscribed_rejected(self, _mock: MagicMock) -> None:
        """Subscribed non-admin is rejected."""
        update = _make_update(user_id=_NON_ADMIN_ID, text="/removechannel")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_unsubscribed_gets_lock(self, _mock: MagicMock) -> None:
        """Unsubscribed non-admin gets lock message."""
        _setup_channels([_CHANNEL_A])
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="left"),
        )
        update = _make_update(user_id=_NON_ADMIN_ID, text="/removechannel")
        ctx = _make_context(bot)

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        self.assertTrue(is_locked(_NON_ADMIN_ID))
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("غير مشترك", reply)


# ── Tests: removechannel_select ───────────────────────────────────────


class TestRemovechannelSelect(unittest.IsolatedAsyncioTestCase):
    """Tests for the removechannel_select callback handler."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_valid_slug_shows_confirmation(self, _mock: MagicMock) -> None:
        """Selecting a valid channel shows confirmation buttons."""
        _setup_channels([_CHANNEL_A])
        update = _make_callback(_TEST_ADMIN_ID, "rmch:ch_a")
        ctx = _make_context()

        result = await removechannel_select(update, ctx)

        self.assertEqual(result, REMOVECHANNEL_CONFIRM)
        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Channel A", text)
        self.assertIn("أنت على وشك", text)
        # Check confirmation buttons exist
        markup = update.callback_query.edit_message_text.call_args[1].get(
            "reply_markup",
        )
        flat = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0].callback_data, "rmch_yes:ch_a")
        self.assertEqual(flat[1].callback_data, "rmch_no")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_stale_slug_handled(self, _mock: MagicMock) -> None:
        """Clicking a button for a deleted channel shows safe message."""
        CHANNELS.clear()  # simulate channel already removed
        update = _make_callback(_TEST_ADMIN_ID, "rmch:ch_a")
        ctx = _make_context()

        result = await removechannel_select(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("لم تعد متاحة", text)

    async def test_unauthorized_callback_rejected(self) -> None:
        """Non-admin clicking a callback is rejected."""
        _setup_channels([_CHANNEL_A])
        update = _make_callback(_NON_ADMIN_ID, "rmch:ch_a")
        ctx = _make_context()

        result = await removechannel_select(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)


# ── Tests: removechannel_confirm ──────────────────────────────────────


class TestRemovechannelConfirm(unittest.IsolatedAsyncioTestCase):
    """Tests for the removechannel_confirm callback handler."""

    def setUp(self) -> None:
        self.test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.test_db_path = self.test_db.name
        self.test_db.close()
        import db
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.test_db_path
        db.init_db(db.DB_PATH)
        CHANNELS.clear()

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

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_confirm_deletes_selected_channel(self, _mock: MagicMock) -> None:
        """Confirming deletes exactly the selected channel."""
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        # Persist to SQLite too
        import db
        db.save_channel(_CHANNEL_A, self.test_db_path)
        db.save_channel(_CHANNEL_B, self.test_db_path)

        update = _make_callback(_TEST_ADMIN_ID, "rmch_yes:ch_a")
        ctx = _make_context()

        result = await removechannel_confirm(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        # ch_a removed, ch_b still present
        self.assertNotIn("ch_a", CHANNELS)
        self.assertIn("ch_b", CHANNELS)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("تم حذف القناة", text)
        self.assertIn("Channel A", text)
        # Verify SQLite
        retrieved = db.get_channel_from_db("ch_a", self.test_db_path)
        self.assertIsNone(retrieved)
        still_there = db.get_channel_from_db("ch_b", self.test_db_path)
        self.assertIsNotNone(still_there)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_confirm_stale_slug(self, _mock: MagicMock) -> None:
        """Confirming a deleted channel shows safe message."""
        CHANNELS.clear()
        update = _make_callback(_TEST_ADMIN_ID, "rmch_yes:ch_a")
        ctx = _make_context()

        result = await removechannel_confirm(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("لم تعد متاحة", text)

    async def test_unauthorized_confirm_rejected(self) -> None:
        """Non-admin confirming is rejected."""
        _setup_channels([_CHANNEL_A])
        update = _make_callback(_NON_ADMIN_ID, "rmch_yes:ch_a")
        ctx = _make_context()

        result = await removechannel_confirm(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)


# ── Tests: removechannel_cancel_cb ────────────────────────────────────


class TestRemovechannelCancelCb(unittest.IsolatedAsyncioTestCase):
    """Tests for the cancel callback handler."""

    async def test_cancel_shows_message(self) -> None:
        update = _make_callback(_TEST_ADMIN_ID, "rmch_no")
        ctx = _make_context()

        result = await removechannel_cancel_cb(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("تم الإلغاء", text)


# ── Tests: removechannel_cancel (text /cancel) ────────────────────────


class TestRemovechannelCancel(unittest.IsolatedAsyncioTestCase):
    """Tests for the /cancel text fallback."""

    async def test_cancel_reply(self) -> None:
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/cancel")
        ctx = _make_context()

        result = await removechannel_cancel(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("تم الإلغاء", reply)


if __name__ == "__main__":
    unittest.main()
