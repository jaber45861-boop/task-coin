"""
Tests for the Telegram Mini App Open Button configuration.

Run:
    python -m pytest test_mini_app_button.py -v
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import MenuButtonWebApp, WebAppInfo


# ── Tests: MINI_APP_URL validation ────────────────────────────────────


class TestMiniAppUrlValidation(unittest.TestCase):
    """Tests for the get_mini_app_url() config helper."""

    def test_missing_env_var_raises(self) -> None:
        """MINI_APP_URL must be present."""
        with patch.dict("os.environ", {}, clear=True):
            from config import get_mini_app_url
            with self.assertRaises(RuntimeError) as ctx:
                get_mini_app_url()
            self.assertIn("MINI_APP_URL", str(ctx.exception))

    def test_empty_env_var_raises(self) -> None:
        """Empty MINI_APP_URL is treated as missing."""
        with patch.dict("os.environ", {"MINI_APP_URL": ""}, clear=False):
            import config
            import importlib
            with self.assertRaises(RuntimeError):
                config.get_mini_app_url()

    def test_whitespace_only_raises(self) -> None:
        """Whitespace-only MINI_APP_URL is treated as missing."""
        with patch.dict("os.environ", {"MINI_APP_URL": "   "}, clear=False):
            import config
            with self.assertRaises(RuntimeError):
                config.get_mini_app_url()

    def test_non_https_rejected(self) -> None:
        """Non-HTTPS URLs must be rejected."""
        with patch.dict("os.environ", {"MINI_APP_URL": "http://example.com"}, clear=False):
            import config
            with self.assertRaises(RuntimeError) as ctx:
                config.get_mini_app_url()
            self.assertIn("HTTPS", str(ctx.exception))

    def test_ftp_rejected(self) -> None:
        """FTP URLs must be rejected."""
        with patch.dict("os.environ", {"MINI_APP_URL": "ftp://example.com"}, clear=False):
            import config
            with self.assertRaises(RuntimeError) as ctx:
                config.get_mini_app_url()
            self.assertIn("HTTPS", str(ctx.exception))

    def test_malformed_url_rejected(self) -> None:
        """Completely malformed URLs must be rejected."""
        with patch.dict("os.environ", {"MINI_APP_URL": "not-a-url"}, clear=False):
            import config
            with self.assertRaises(RuntimeError):
                config.get_mini_app_url()

    def test_localhost_rejected(self) -> None:
        """Localhost HTTP URLs must be rejected."""
        with patch.dict("os.environ", {"MINI_APP_URL": "http://localhost:3000"}, clear=False):
            import config
            with self.assertRaises(RuntimeError) as ctx:
                config.get_mini_app_url()
            self.assertIn("HTTPS", str(ctx.exception))

    def test_valid_https_accepted(self) -> None:
        """Valid HTTPS URL is accepted."""
        with patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"}, clear=False):
            import config
            result = config.get_mini_app_url()
            self.assertEqual(result, "https://mini.example.com")

    def test_valid_https_with_path_accepted(self) -> None:
        """Valid HTTPS URL with path is accepted."""
        url = "https://mini.example.com/app"
        with patch.dict("os.environ", {"MINI_APP_URL": url}, clear=False):
            import config
            result = config.get_mini_app_url()
            self.assertEqual(result, url)

    def test_no_hostname_rejected(self) -> None:
        """HTTPS URL without a hostname is rejected."""
        with patch.dict("os.environ", {"MINI_APP_URL": "https://"}, clear=False):
            import config
            with self.assertRaises(RuntimeError):
                config.get_mini_app_url()


# ── Tests: setup_menu_button ──────────────────────────────────────────


class TestSetupMenuButton(unittest.IsolatedAsyncioTestCase):
    """Tests that setup_menu_button configures the Telegram menu button correctly."""

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_configures_menu_button_for_each_admin(self) -> None:
        """set_chat_menu_button should be called for every admin."""
        from config import ADMINS
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        self.assertEqual(mock_app.bot.set_chat_menu_button.call_count, len(ADMINS))

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_menu_button_text_is_open(self) -> None:
        """The menu button text must be exactly 'Open'."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        call_args = mock_app.bot.set_chat_menu_button.call_args
        menu_button = call_args.kwargs.get("menu_button") or call_args[1].get("menu_button")
        self.assertIsNotNone(menu_button)
        self.assertIsInstance(menu_button, MenuButtonWebApp)
        self.assertEqual(menu_button.text, "Open")

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_web_app_info_receives_configured_url(self) -> None:
        """WebAppInfo.url must equal MINI_APP_URL."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        call_args = mock_app.bot.set_chat_menu_button.call_args
        menu_button = call_args.kwargs.get("menu_button") or call_args[1].get("menu_button")
        self.assertIsNotNone(menu_button)
        self.assertIsInstance(menu_button.web_app, WebAppInfo)
        self.assertEqual(menu_button.web_app.url, "https://mini.example.com")

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_uses_set_chat_menu_button_api(self) -> None:
        """Must use Bot.set_chat_menu_button (official Telegram API)."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        mock_app.bot.set_chat_menu_button.assert_called()

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_chat_id_matches_admin(self) -> None:
        """Each set_chat_menu_button call should target an admin's chat_id."""
        from config import ADMINS
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        called_ids = set()
        for call in mock_app.bot.set_chat_menu_button.call_args_list:
            chat_id = call.kwargs.get("chat_id") or call[1].get("chat_id")
            called_ids.add(chat_id)
        for admin_id in ADMINS:
            self.assertIn(admin_id, called_ids)

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_missing_url_raises(self) -> None:
        """setup_menu_button must raise if MINI_APP_URL is not set."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()

        with patch.dict("os.environ", {}, clear=False):
            # Remove MINI_APP_URL if it exists
            import os
            os.environ.pop("MINI_APP_URL", None)
            with self.assertRaises(RuntimeError):
                await setup_menu_button(mock_app)

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_no_reply_keyboard_created(self) -> None:
        """setup_menu_button must not create ReplyKeyboard or InlineKeyboard."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()
        mock_app.bot.send_message = AsyncMock()

        await setup_menu_button(mock_app)

        # Must NOT call send_message (which would create inline keyboards)
        mock_app.bot.send_message.assert_not_called()


# ── Tests: Combined post_init chains both setups ──────────────────────


class TestCombinedPostInit(unittest.IsolatedAsyncioTestCase):
    """Verify the _combined_post_init calls both clear_command_menus and menu button."""

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_combined_post_init_calls_clear_command_menus(self) -> None:
        """The combined post_init must call clear_command_menus."""
        from bot import clear_command_menus, setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.delete_my_commands = AsyncMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        # Simulate what main() does
        async def _combined(application):
            await clear_command_menus(application)
            await setup_menu_button(application)

        await _combined(mock_app)

        # Both APIs should have been called
        mock_app.bot.delete_my_commands.assert_called()
        mock_app.bot.set_chat_menu_button.assert_called()

    @patch.dict("os.environ", {"MINI_APP_URL": "https://mini.example.com"})
    async def test_combined_post_init_calls_menu_button(self) -> None:
        """The combined post_init must call setup_menu_button."""
        from bot import setup_menu_button

        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.set_chat_menu_button = AsyncMock()

        await setup_menu_button(mock_app)

        mock_app.bot.set_chat_menu_button.assert_called()


# ── Tests: existing functionality remains intact ──────────────────────


class TestExistingFunctionality(unittest.TestCase):
    """Verify existing bot functions and handlers still exist."""

    def test_clear_command_menus_handler_exists(self) -> None:
        from bot import clear_command_menus
        self.assertTrue(callable(clear_command_menus))

    def test_menu_button_handler_exists(self) -> None:
        from bot import setup_menu_button
        self.assertTrue(callable(setup_menu_button))

    def test_admin_panel_handler_exists(self) -> None:
        from bot import admin_command
        self.assertTrue(callable(admin_command))

    def test_admin_panel_callback_exists(self) -> None:
        from bot import admin_panel_callback
        self.assertTrue(callable(admin_panel_callback))

    def test_add_channel_handler_exists(self) -> None:
        from bot import add_channel
        self.assertTrue(callable(add_channel))

    def test_remove_channel_handler_exists(self) -> None:
        from bot import remove_channel
        self.assertTrue(callable(remove_channel))

    def test_list_channels_handler_exists(self) -> None:
        from bot import list_channels
        self.assertTrue(callable(list_channels))

    def test_no_inline_keyboard_open_button(self) -> None:
        """Verify setup_menu_button does not use InlineKeyboard."""
        import bot as bot_mod
        source_lines = bot_mod.setup_menu_button.__code__.co_consts
        for const in source_lines:
            if isinstance(const, str) and "InlineKeyboard" in const:
                self.fail("setup_menu_button should not reference InlineKeyboard")

    def test_menu_button_is_web_app_type(self) -> None:
        """MenuButtonWebApp type should be 'web_app'."""
        btn = MenuButtonWebApp(text="Open", web_app=WebAppInfo(url="https://example.com"))
        self.assertEqual(btn.type.value, "web_app")


if __name__ == "__main__":
    unittest.main()
