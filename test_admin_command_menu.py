"""
Tests for the admin-only Telegram command menu.

Run:
    python -m pytest test_admin_command_menu.py -v
    # or
    python -m unittest test_admin_command_menu.py -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import ADMINS
from bot import ADMIN_COMMANDS, setup_admin_command_menu


class TestAdminCommandMenu(unittest.IsolatedAsyncioTestCase):
    """Tests that the admin command menu is configured correctly."""

    def test_admin_commands_constant(self) -> None:
        """ADMIN_COMMANDS should contain exactly the three management commands."""
        self.assertEqual(len(ADMIN_COMMANDS), 3)

        command_names = [cmd.command for cmd in ADMIN_COMMANDS]
        self.assertIn("addchannel", command_names)
        self.assertIn("editchannel", command_names)
        self.assertIn("removechannel", command_names)

    def test_admin_commands_have_arabic_descriptions(self) -> None:
        """Each admin command should have an Arabic description."""
        for cmd in ADMIN_COMMANDS:
            self.assertIsInstance(cmd, BotCommand)
            # Arabic text contains Unicode Arabic characters (U+0600–U+06FF)
            has_arabic = any("\u0600" <= c <= "\u06FF" for c in cmd.description)
            self.assertTrue(
                has_arabic,
                f"Command /{cmd.command} missing Arabic description: {cmd.description!r}",
            )

    def test_admin_command_slugs(self) -> None:
        """Verify exact slug and Arabic text for each admin command."""
        cmd_map = {cmd.command: cmd.description for cmd in ADMIN_COMMANDS}
        self.assertEqual(cmd_map["addchannel"], "إضافة قناة")
        self.assertEqual(cmd_map["editchannel"], "تعديل قناة")
        self.assertEqual(cmd_map["removechannel"], "حذف قناة")

    async def test_setup_sets_admin_scope_for_each_admin(self) -> None:
        """setup_admin_command_menu should call set_my_commands for every admin."""
        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_my_commands = AsyncMock()

        await setup_admin_command_menu(mock_app)

        # One call for default scope + one per admin
        expected_calls = 1 + len(ADMINS)
        self.assertEqual(mock_app.bot.set_my_commands.call_count, expected_calls)

        # First call is the default scope (empty)
        first_call = mock_app.bot.set_my_commands.call_args_list[0]
        scope_arg = first_call[1].get("scope") or first_call[0][1] if len(first_call[0]) > 1 else first_call[1].get("scope")
        self.assertIsInstance(scope_arg, BotCommandScopeDefault)

    async def test_setup_uses_bot_command_scope_chat_for_admins(self) -> None:
        """Each admin gets a BotCommandScopeChat with their chat_id."""
        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_my_commands = AsyncMock()

        await setup_admin_command_menu(mock_app)

        # Check all calls after the first (default scope) one
        for admin_id in ADMINS:
            found = False
            for call in mock_app.bot.set_my_commands.call_args_list:
                kwargs = call[1]
                scope = kwargs.get("scope")
                if (
                    isinstance(scope, BotCommandScopeChat)
                    and scope.chat_id == admin_id
                ):
                    found = True
                    # Verify the commands passed are ADMIN_COMMANDS
                    cmds = kwargs["commands"]
                    self.assertEqual(len(cmds), 3)
                    names = [c.command for c in cmds]
                    self.assertIn("addchannel", names)
                    self.assertIn("editchannel", names)
                    self.assertIn("removechannel", names)
                    break
            self.assertTrue(
                found,
                f"set_my_commands was not called with BotCommandScopeChat "
                f"for admin {admin_id}",
            )

    async def test_default_scope_sends_empty_commands(self) -> None:
        """Default scope should clear commands (normal users see nothing)."""
        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_my_commands = AsyncMock()

        await setup_admin_command_menu(mock_app)

        first_call = mock_app.bot.set_my_commands.call_args_list[0]
        cmds = first_call.kwargs.get("commands", first_call.args[0] if first_call.args else None)
        self.assertEqual(cmds, [])

    async def test_admin_menu_does_not_contain_listchannels(self) -> None:
        """The admin menu should NOT include /listchannels (it's direct-text only)."""
        command_names = [cmd.command for cmd in ADMIN_COMMANDS]
        self.assertNotIn("listchannels", command_names)


class TestCommandHandlersIntact(unittest.TestCase):
    """Verify existing command handlers still exist and are callable."""

    def test_add_channel_handler_exists(self) -> None:
        """add_channel function should still be importable from bot."""
        from bot import add_channel
        self.assertTrue(callable(add_channel))

    def test_remove_channel_handler_exists(self) -> None:
        """remove_channel function should still be importable from bot."""
        from bot import remove_channel
        self.assertTrue(callable(remove_channel))

    def test_list_channels_handler_exists(self) -> None:
        """list_channels function should still be importable from bot."""
        from bot import list_channels
        self.assertTrue(callable(list_channels))

    def test_verify_subscription_handler_exists(self) -> None:
        """verify_subscription callback handler should still exist."""
        from bot import verify_subscription
        self.assertTrue(callable(verify_subscription))

    def test_start_handler_exists(self) -> None:
        """start entry point should still exist."""
        from bot import start
        self.assertTrue(callable(start))

    def test_cancel_handler_exists(self) -> None:
        """cancel fallback handler should still exist."""
        from bot import cancel
        self.assertTrue(callable(cancel))

    def test_subscription_gate_exists(self) -> None:
        """subscription_gate should still exist."""
        from bot import subscription_gate
        self.assertTrue(callable(subscription_gate))

    def test_subscription_message_gate_exists(self) -> None:
        """subscription_message_gate should still exist."""
        from bot import subscription_message_gate
        self.assertTrue(callable(subscription_message_gate))

    def test_on_chat_member_update_exists(self) -> None:
        """on_chat_member_update should still exist."""
        from bot import on_chat_member_update
        self.assertTrue(callable(on_chat_member_update))

    def test_admin_commands_are_new(self) -> None:
        """editchannel is registered in the menu but has no handler yet —
        that's expected per the task scope (do not implement edit workflow)."""
        import bot as bot_mod

        # addchannel, removechannel, listchannels all have handlers
        self.assertTrue(callable(bot_mod.add_channel))
        self.assertTrue(callable(bot_mod.remove_channel))
        self.assertTrue(callable(bot_mod.list_channels))

        # editchannel should NOT have a handler function yet
        self.assertFalse(hasattr(bot_mod, "editchannel"))


if __name__ == "__main__":
    unittest.main()
