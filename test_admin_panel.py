"""
Tests for the inline Admin Channel Panel.

Run:
    python -m pytest test_admin_panel.py -v
    # or
    python -m unittest test_admin_panel.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from config import ADMINS, CHANNELS, Channel, is_admin
from subscription import is_locked, lock_user, unlock_user
from bot import (
    ADDCHANNEL_TITLE,
    ADDCHANNEL_USERNAME,
    REMOVECHANNEL_SELECT,
    admin_command,
    admin_panel_callback,
    addchannel_start,
    addchannel_username,
    removechannel_start,
    list_channels,
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


# ── Tests: /admin is admin-only ──────────────────────────────────────


class TestAdminCommandAdminOnly(unittest.IsolatedAsyncioTestCase):
    """Tests that /admin command is admin-only."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_admin_sees_panel(self, _mock: MagicMock) -> None:
        """Admin sending /admin sees the inline panel."""
        update = _make_update(user_id=_TEST_ADMIN_ID)
        ctx = _make_context()

        await admin_command(update, ctx)

        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        self.assertEqual(args[0], "⚙️ إدارة القنوات")
        markup = kwargs.get("reply_markup")
        self.assertIsInstance(markup, InlineKeyboardMarkup)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_non_admin_rejected(self, _mock: MagicMock) -> None:
        """Non-admin sending /admin gets rejection message."""
        update = _make_update(user_id=_NON_ADMIN_ID)
        ctx = _make_context()

        await admin_command(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_panel_not_added_to_command_menu(self, _mock: MagicMock) -> None:
        """Verify /admin is NOT in the bot command menu (no ADMIN_COMMANDS)."""
        from bot import admin_command  # noqa: F811

        # admin_command should exist as a handler but not in any command menu
        self.assertTrue(callable(admin_command))

        # Verify ADMIN_COMMANDS no longer exists
        import bot as bot_mod
        self.assertFalse(
            hasattr(bot_mod, "ADMIN_COMMANDS"),
            "ADMIN_COMMANDS should have been removed",
        )
        self.assertFalse(
            hasattr(bot_mod, "setup_admin_command_menu"),
            "setup_admin_command_menu should have been removed",
        )


# ── Tests: Panel contains exactly 3 buttons ───────────────────────────


class TestAdminPanelButtons(unittest.IsolatedAsyncioTestCase):
    """Tests that the admin panel has exactly 3 inline buttons."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_exactly_three_buttons(self, _mock: MagicMock) -> None:
        """Panel must contain exactly 3 inline buttons."""
        update = _make_update(user_id=_TEST_ADMIN_ID)
        ctx = _make_context()

        await admin_command(update, ctx)

        markup = update.message.reply_text.call_args[1]["reply_markup"]
        flat = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(len(flat), 3)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_button_texts_and_data(self, _mock: MagicMock) -> None:
        """Each button has the correct text and callback_data."""
        update = _make_update(user_id=_TEST_ADMIN_ID)
        ctx = _make_context()

        await admin_command(update, ctx)

        markup = update.message.reply_text.call_args[1]["reply_markup"]
        flat = [btn for row in markup.inline_keyboard for btn in row]
        self.assertEqual(flat[0].text, "➕ إضافة قناة أو مجموعة")
        self.assertEqual(flat[0].callback_data, "admin_panel:add")
        self.assertEqual(flat[1].text, "🗑️ حذف قناة")
        self.assertEqual(flat[1].callback_data, "admin_panel:remove")
        self.assertEqual(flat[2].text, "📋 عرض القنوات")
        self.assertEqual(flat[2].callback_data, "admin_panel:list")


# ── Tests: Normal users cannot see admin panel ────────────────────────


class TestNormalUserBlocked(unittest.IsolatedAsyncioTestCase):
    """Tests that normal users cannot open the admin panel."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_normal_user_panel_blocked(self, _mock: MagicMock) -> None:
        """Normal user sending /admin gets rejection."""
        update = _make_update(user_id=_NON_ADMIN_ID)
        ctx = _make_context()

        await admin_command(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", reply)


# ── Tests: Unauthorized callbacks cannot trigger admin actions ────────


class TestUnauthorizedCallbacks(unittest.IsolatedAsyncioTestCase):
    """Tests that unauthorized callback queries are rejected."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unauthorized_list_callback(self, _mock: MagicMock) -> None:
        """Non-admin clicking list button is rejected."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unauthorized_add_callback(self, _mock: MagicMock) -> None:
        """Non-admin clicking add button is rejected by admin_panel_callback."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_unauthorized_remove_callback(self, _mock: MagicMock) -> None:
        """Non-admin clicking remove button is rejected by admin_panel_callback."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:remove")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)


# ── Tests: Add button reaches existing add workflow ───────────────────


class TestAddButtonReachesWorkflow(unittest.IsolatedAsyncioTestCase):
    """Tests that the admin panel Add button enters the addchannel workflow."""

    def setUp(self) -> None:
        unlock_user(_TEST_ADMIN_ID)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_button_enters_addchannel_flow(self, _mock: MagicMock) -> None:
        """Clicking Add button routes to addchannel_start via ConversationHandler."""
        # The ConversationHandler entry point calls addchannel_start directly.
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        self.assertTrue(update.callback_query.answer.called)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Username", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_button_cleans_previous_state(self, _mock: MagicMock) -> None:
        """Starting from panel clears leftover user_data."""
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()
        ctx.user_data["addchannel_channel_id"] = -999
        ctx.user_data["addchannel_username"] = "stale"

        await addchannel_start(update, ctx)

        self.assertNotIn("addchannel_channel_id", ctx.user_data)
        self.assertNotIn("addchannel_username", ctx.user_data)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_button_unsubscribed_non_admin(self, _mock: MagicMock) -> None:
        """Non-admin clicking add button is rejected by admin_panel_callback."""
        ch = Channel(
            slug="ch1", channel_id=-100111,
            username="ch1user", title="Ch1", required=True,
        )
        CHANNELS["ch1"] = ch

        bot = MagicMock()
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="left"),
        )
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:add")
        ctx = _make_context(bot)

        await admin_panel_callback(update, ctx)

        # admin_panel_callback rejects non-admins before routing to addchannel_start
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)


# ── Tests: Delete button reaches existing delete workflow ─────────────


class TestDeleteButtonReachesWorkflow(unittest.IsolatedAsyncioTestCase):
    """Tests that the admin panel Delete button enters the removechannel workflow."""

    def setUp(self) -> None:
        unlock_user(_TEST_ADMIN_ID)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_delete_button_enters_removechannel_flow(
        self, _mock: MagicMock
    ) -> None:
        """Clicking Delete button with channels shows selection buttons."""
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:remove")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, REMOVECHANNEL_SELECT)
        update.callback_query.answer.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("اختر القناة", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_delete_button_no_channels(self, _mock: MagicMock) -> None:
        """Clicking Delete with no channels shows empty message."""
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:remove")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("لا توجد قنوات", text)


# ── Tests: List button reaches existing list behavior ─────────────────


class TestListButtonReachesListBehavior(unittest.IsolatedAsyncioTestCase):
    """Tests that the admin panel List button shows channel list."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_list_button_shows_channels(self, _mock: MagicMock) -> None:
        """Clicking List button with channels shows the list."""
        _setup_channels([_CHANNEL_A, _CHANNEL_B])
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Channel A", text)
        self.assertIn("Channel B", text)
        self.assertIn("ch_a", text)
        self.assertIn("ch_b", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_list_button_empty(self, _mock: MagicMock) -> None:
        """Clicking List button with no channels shows empty message."""
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("لا توجد قنوات", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_list_button_parse_mode(self, _mock: MagicMock) -> None:
        """List response uses Markdown parse mode."""
        _setup_channels([_CHANNEL_A])
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        kwargs = update.callback_query.edit_message_text.call_args[1]
        self.assertEqual(kwargs.get("parse_mode"), "Markdown")


# ── Tests: No native command-menu registration used ───────────────────


class TestNoNativeCommandMenu(unittest.TestCase):
    """Tests that native admin command-menu registration is no longer used."""

    def test_admin_commands_removed(self) -> None:
        """ADMIN_COMMANDS should not exist in bot module."""
        import bot as bot_mod
        self.assertFalse(hasattr(bot_mod, "ADMIN_COMMANDS"))

    def test_setup_admin_command_menu_removed(self) -> None:
        """setup_admin_command_menu should not exist in bot module."""
        import bot as bot_mod
        self.assertFalse(hasattr(bot_mod, "setup_admin_command_menu"))

    def test_bot_command_scopes_used_for_deletion(self) -> None:
        """BotCommandScopeChat and BotCommandScopeDefault are imported for deletion."""
        import bot as bot_mod
        import inspect
        source = inspect.getsource(bot_mod)
        self.assertIn("BotCommandScopeChat", source)
        self.assertIn("BotCommandScopeDefault", source)

    def test_post_init_set_to_clear_command_menus(self) -> None:
        """app.post_init should be set to clear_command_menus, not setup_admin_command_menu."""
        import bot as bot_mod
        import inspect
        source = inspect.getsource(bot_mod.main)
        self.assertIn("clear_command_menus", source)
        self.assertNotIn("setup_admin_command_menu", source)


# ── Tests: Command menu cleanup on startup ──────────────────────────


class TestClearCommandMenus(unittest.IsolatedAsyncioTestCase):
    """Tests that clear_command_menus deletes old commands from Telegram."""

    async def test_clears_default_scope(self) -> None:
        """clear_command_menus calls delete_my_commands for BotCommandScopeDefault."""
        from bot import clear_command_menus

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.delete_my_commands = AsyncMock()

        await clear_command_menus(mock_app)

        # Should be called once for default scope
        default_calls = [
            call for call in mock_app.bot.delete_my_commands.call_args_list
            if call.kwargs.get("scope") is not None
            and type(call.kwargs["scope"]).__name__ == "BotCommandScopeDefault"
        ]
        self.assertEqual(len(default_calls), 1)

    async def test_clears_admin_scopes(self) -> None:
        """clear_command_menus calls delete_my_commands for each admin BotCommandScopeChat."""
        from bot import clear_command_menus
        from config import ADMINS

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.delete_my_commands = AsyncMock()

        await clear_command_menus(mock_app)

        # Should be called once per admin + once for default
        expected_calls = 1 + len(ADMINS)
        self.assertEqual(
            mock_app.bot.delete_my_commands.call_count, expected_calls
        )

        # Verify each admin gets a BotCommandScopeChat call
        for admin_id in ADMINS:
            found = False
            for call in mock_app.bot.delete_my_commands.call_args_list:
                scope = call.kwargs.get("scope")
                if scope is not None and getattr(scope, "chat_id", None) == admin_id:
                    found = True
                    break
            self.assertTrue(
                found,
                f"delete_my_commands not called with BotCommandScopeChat "
                f"for admin {admin_id}",
            )

    async def test_no_set_my_commands_called(self) -> None:
        """clear_command_menus should NOT call set_my_commands."""
        from bot import clear_command_menus

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.delete_my_commands = AsyncMock()
        mock_app.bot.set_my_commands = AsyncMock()

        await clear_command_menus(mock_app)

        mock_app.bot.set_my_commands.assert_not_called()

    async def test_clear_command_menus_is_callable(self) -> None:
        """clear_command_menus should be importable and callable."""
        from bot import clear_command_menus
        self.assertTrue(callable(clear_command_menus))

    def test_no_replacement_menu_registered(self) -> None:
        """No ADMIN_COMMANDS or set_my_commands registration should exist."""
        import bot as bot_mod
        import inspect
        source = inspect.getsource(bot_mod)
        self.assertNotIn("set_my_commands", source)
        self.assertNotIn("ADMIN_COMMANDS", source)
        self.assertNotIn("setup_admin_command_menu", source)


# ── Tests: Existing direct commands still work ────────────────────────


class TestExistingCommandsStillWork(unittest.TestCase):
    """Verify existing command handlers still exist and are callable."""

    def test_add_channel_handler_exists(self) -> None:
        from bot import add_channel
        self.assertTrue(callable(add_channel))

    def test_remove_channel_handler_exists(self) -> None:
        from bot import remove_channel
        self.assertTrue(callable(remove_channel))

    def test_list_channels_handler_exists(self) -> None:
        from bot import list_channels
        self.assertTrue(callable(list_channels))

    def test_verify_subscription_handler_exists(self) -> None:
        from bot import verify_subscription
        self.assertTrue(callable(verify_subscription))

    def test_start_handler_exists(self) -> None:
        from bot import start
        self.assertTrue(callable(start))

    def test_cancel_handler_exists(self) -> None:
        from bot import cancel
        self.assertTrue(callable(cancel))

    def test_subscription_gate_exists(self) -> None:
        from bot import subscription_gate
        self.assertTrue(callable(subscription_gate))

    def test_subscription_message_gate_exists(self) -> None:
        from bot import subscription_message_gate
        self.assertTrue(callable(subscription_message_gate))

    def test_on_chat_member_update_exists(self) -> None:
        from bot import on_chat_member_update
        self.assertTrue(callable(on_chat_member_update))

    def test_addchannel_start_exists(self) -> None:
        from bot import addchannel_start
        self.assertTrue(callable(addchannel_start))

    def test_removechannel_start_exists(self) -> None:
        from bot import removechannel_start
        self.assertTrue(callable(removechannel_start))

    def test_admin_command_exists(self) -> None:
        from bot import admin_command
        self.assertTrue(callable(admin_command))

    def test_admin_panel_callback_exists(self) -> None:
        from bot import admin_panel_callback
        self.assertTrue(callable(admin_panel_callback))


# ── Tests: No ReplyKeyboard introduced ────────────────────────────────


class TestNoReplyKeyboard(unittest.TestCase):
    """Verify no ReplyKeyboard was introduced."""

    def test_no_reply_keyboard_in_bot(self) -> None:
        """bot.py should not import or use ReplyKeyboard."""
        import bot as bot_mod
        import inspect
        source = inspect.getsource(bot_mod)
        self.assertNotIn("ReplyKeyboard", source)
        self.assertNotIn("ReplyKeyboardMarkup", source)
        self.assertNotIn("ReplyKeyboardRemove", source)


# ── Tests: Existing functionality remains intact ──────────────────────


class TestExistingFunctionalityIntact(unittest.IsolatedAsyncioTestCase):
    """Verify existing functionality is preserved."""

    def setUp(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_ADMIN_ID)
        unlock_user(_NON_ADMIN_ID)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_addchannel_command_still_works(self, _mock: MagicMock) -> None:
        """Direct /addchannel command still triggers addchannel_start."""
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/addchannel")
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Username", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_removechannel_command_still_works(
        self, _mock: MagicMock
    ) -> None:
        """Direct /removechannel command still triggers removechannel_start."""
        _setup_channels([_CHANNEL_A])
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/removechannel")
        ctx = _make_context()

        result = await removechannel_start(update, ctx)

        self.assertEqual(result, REMOVECHANNEL_SELECT)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_listchannels_command_still_works(
        self, _mock: MagicMock
    ) -> None:
        """Direct /listchannels command still triggers list_channels."""
        _setup_channels([_CHANNEL_A])
        update = _make_update(user_id=_TEST_ADMIN_ID, text="/listchannels")
        ctx = _make_context()

        await list_channels(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("Channel A", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_removechannel_direct_slug_still_works(
        self, _mock: MagicMock
    ) -> None:
        """Direct /removechannel <slug> still performs direct deletion."""
        import tempfile
        import os
        import db as _db

        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = test_db.name
        test_db.close()
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
    async def test_anti_bot_still_works(self, _mock: MagicMock) -> None:
        """The /start anti-bot conversation still works."""
        from bot import start
        import db as _db

        # Persist a language so /start goes straight to the Anti-Bot step.
        _db.init_db()
        _db.register_user(_TEST_ADMIN_ID, None, None)
        _db.set_user_language(_TEST_ADMIN_ID, "ar")

        update = _make_update(user_id=_TEST_ADMIN_ID, text="/start")
        ctx = _make_context()

        result = await start(update, ctx)

        self.assertEqual(result, 0)  # ANTI_BOT state
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("لست بوت", reply)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_sqlite_persistence_intact(self, _mock: MagicMock) -> None:
        """Channels still persist to SQLite."""
        import tempfile
        import os
        import db as _db

        test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = test_db.name
        test_db.close()
        orig = _db.DB_PATH
        _db.DB_PATH = db_path
        _db.init_db(db_path)
        try:
            _db.save_channel(_CHANNEL_A, db_path)
            retrieved = _db.get_channel_from_db("ch_a", db_path)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.channel_id, -100111)
        finally:
            _db.DB_PATH = orig
            if os.path.exists(db_path):
                os.unlink(db_path)
            for sfx in ("-wal", "-shm"):
                p = db_path + sfx
                if os.path.exists(p):
                    os.unlink(p)


# ── Tests: Admin panel admin-only callback verification ───────────────


class TestPanelCallbackAdminVerification(unittest.IsolatedAsyncioTestCase):
    """Tests that every panel callback independently verifies admin."""

    def setUp(self) -> None:
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_list_callback_independently_checks_admin(
        self, _mock: MagicMock
    ) -> None:
        """List callback checks admin even if user somehow reached it."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_callback_independently_checks_admin(
        self, _mock: MagicMock
    ) -> None:
        """Add callback checks admin independently via admin_panel_callback."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_remove_callback_independently_checks_admin(
        self, _mock: MagicMock
    ) -> None:
        """Remove callback checks admin independently via admin_panel_callback."""
        update = _make_callback(_NON_ADMIN_ID, "admin_panel:remove")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", text)


# ── Tests: admin_panel_callback routes add/remove correctly ──────────


class TestAdminPanelCallbackRoutesAdd(unittest.IsolatedAsyncioTestCase):
    """Test that admin_panel_callback properly routes the add callback."""

    def setUp(self) -> None:
        unlock_user(_TEST_ADMIN_ID)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_callback_enters_addchannel_conv(
        self, _mock: MagicMock
    ) -> None:
        """admin_panel:add callback enters addchannel_start via ConversationHandler entry point."""
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        # addchannel_start returns ADDCHANNEL_USERNAME, which the ConversationHandler
        # uses to transition to the username-input state.
        self.assertEqual(result, ADDCHANNEL_USERNAME)
        self.assertTrue(update.callback_query.answer.called)
        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Username", text)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_button_returns_correct_state(
        self, _mock: MagicMock
    ) -> None:
        """admin_panel:add returns ADDCHANNEL_USERNAME via addchannel_start."""
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()

        result = await addchannel_start(update, ctx)

        self.assertEqual(result, ADDCHANNEL_USERNAME)

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_list_button_still_works_via_callback(
        self, _mock: MagicMock
    ) -> None:
        """admin_panel:list still shows the channel list."""
        _setup_channels([_CHANNEL_A])
        update = _make_callback(_TEST_ADMIN_ID, "admin_panel:list")
        ctx = _make_context()

        await admin_panel_callback(update, ctx)

        text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Channel A", text)



# ── Tests: Full flow admin_panel:add → username/link → addchannel_username ──


class TestAdminPanelAddFullFlow(unittest.IsolatedAsyncioTestCase):
    """Test the full flow: admin_panel:add callback → username/link input → addchannel_username.

    This verifies that the ConversationHandler entry point for admin_panel:add
    properly transitions through the addchannel conversation states.
    """

    def setUp(self) -> None:
        unlock_user(_TEST_ADMIN_ID)
        CHANNELS.clear()

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_then_at_username_advances(self, _mock: MagicMock) -> None:
        """admin_panel:add → @username → advances to ADDCHANNEL_TITLE."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100999
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        # Step 1: admin_panel:add → addchannel_start returns ADDCHANNEL_USERNAME
        start_update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context(bot)
        result = await addchannel_start(start_update, ctx)
        self.assertEqual(result, ADDCHANNEL_USERNAME)

        # Step 2: user sends @username → addchannel_username returns ADDCHANNEL_TITLE
        msg_update = _make_update(_TEST_ADMIN_ID, text="@testchannel")
        result = await addchannel_username(msg_update, ctx)
        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100999)
        self.assertEqual(ctx.user_data["addchannel_username"], "testchannel")
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "channel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_then_tme_link_advances(self, _mock: MagicMock) -> None:
        """admin_panel:add → t.me link → advances to ADDCHANNEL_TITLE."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100777
        mock_chat.type = "supergroup"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        # Step 1: admin_panel:add → addchannel_start
        start_update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context(bot)
        result = await addchannel_start(start_update, ctx)
        self.assertEqual(result, ADDCHANNEL_USERNAME)

        # Step 2: user sends t.me link → addchannel_username
        msg_update = _make_update(_TEST_ADMIN_ID, text="https://t.me/mygroup")
        result = await addchannel_username(msg_update, ctx)
        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100777)
        self.assertEqual(ctx.user_data["addchannel_username"], "mygroup")
        self.assertEqual(ctx.user_data["addchannel_chat_type"], "supergroup")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_then_bare_tme_link_advances(self, _mock: MagicMock) -> None:
        """admin_panel:add → bare t.me/link → advances to ADDCHANNEL_TITLE."""
        bot = MagicMock()
        mock_chat = MagicMock()
        mock_chat.id = -100666
        mock_chat.type = "channel"
        bot.get_chat = AsyncMock(return_value=mock_chat)
        bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator"),
        )

        # Step 1: admin_panel:add
        start_update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context(bot)
        result = await addchannel_start(start_update, ctx)
        self.assertEqual(result, ADDCHANNEL_USERNAME)

        # Step 2: user sends bare t.me link
        msg_update = _make_update(_TEST_ADMIN_ID, text="t.me/testchannel")
        result = await addchannel_username(msg_update, ctx)
        self.assertEqual(result, ADDCHANNEL_TITLE)
        self.assertEqual(ctx.user_data["addchannel_channel_id"], -100666)
        self.assertEqual(ctx.user_data["addchannel_username"], "testchannel")

    @patch("bot.is_admin", side_effect=lambda uid: uid == _TEST_ADMIN_ID)
    async def test_add_then_invalid_rejected_stays(self, _mock: MagicMock) -> None:
        """admin_panel:add → invalid input → stays in ADDCHANNEL_USERNAME."""
        start_update = _make_callback(_TEST_ADMIN_ID, "admin_panel:add")
        ctx = _make_context()
        result = await addchannel_start(start_update, ctx)
        self.assertEqual(result, ADDCHANNEL_USERNAME)

        # User sends invalid input
        msg_update = _make_update(_TEST_ADMIN_ID, text="bad")
        result = await addchannel_username(msg_update, ctx)
        self.assertEqual(result, ADDCHANNEL_USERNAME)
        reply = msg_update.message.reply_text.call_args[0][0]
        self.assertIn("صيغة غير صحيحة", reply)


if __name__ == "__main__":
    unittest.main()
