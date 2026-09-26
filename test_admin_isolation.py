"""
MT-ADMIN-02: AdminNotifier + Telegram isolation hardening.
==========================================================

Covers the required test list for MT-ADMIN-02:

- /start works in a private chat.
- /start does NOT start onboarding in a group chat.
- Anti-bot / subscription gating never send unsolicited group messages.
- Required channel/group stays silent (chat_member handling sends nothing).
- ChatMemberHandler is registered even with zero configured channels.
- Polling explicitly receives chat_member updates via allowed_updates.
- AdminNotifier targets only configured ADMINS.
- Non-admin cannot invoke/use AdminNotifier.
- The dead duplicate subscription_gate is disabled but importable.

Run:
    python -m pytest test_admin_isolation.py -v
"""

from __future__ import annotations

import inspect
import os
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update
from telegram.ext import (
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

import bot as bot_mod
from admin_notifier import (
    AdminNotifier,
    AdminNotifierAuthorizationError,
    AdminNotifierError,
    AdminNotifierTargetError,
)
from config import ADMINS, CHANNELS, Channel, is_admin
from subscription import is_locked, lock_user, unlock_user

_TEST_USER_ID = 555555
_TEST_GROUP_ID = -100999
_REQUIRED_CHANNEL_ID = -100111


# ── Test helpers ─────────────────────────────────────────────────────


def _make_update(
    user_id: int,
    text: str | None = None,
    chat_type: str | None = None,
) -> MagicMock:
    """Build a MagicMock update.  When *chat_type* is given, the update
    positively resolves to that chat type (e.g. "private"/"supergroup");
    otherwise the chat type stays unresolved like the legacy tests."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "tester"
    update.effective_user.first_name = "Tester"
    if chat_type is not None:
        update.effective_chat = MagicMock()
        update.effective_chat.type = chat_type
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = MagicMock()
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = "verify_subscription"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _make_context(bot: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = bot or MagicMock()
    ctx.user_data = {}
    return ctx


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.get_chat_member = AsyncMock()
    return bot


def _real_update(chat_type: str, text: str, command: bool = False) -> Update:
    """Build a real PTB Update so handler *filters* can be evaluated."""
    data: dict = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {
                "id": 42 if chat_type == "private" else _TEST_GROUP_ID,
                "type": chat_type,
            },
            "text": text,
        },
    }
    if command:
        first_word = text.split()[0]
        data["message"]["entities"] = [
            {"type": "bot_command", "offset": 0, "length": len(first_word)}
        ]
    bot = MagicMock()
    bot.username = "test_bot"
    bot.defaults = None  # real tz handling in Message.de_json
    return Update.de_json(data, bot)


def _required_channel() -> Channel:
    return Channel(
        slug="ch_iso",
        channel_id=_REQUIRED_CHANNEL_ID,
        username="ch_iso_user",
        title="Isolation Channel",
        required=True,
    )


def _make_application_mock() -> MagicMock:
    """Build a MagicMock that behaves like a PTB Application lifecycle."""
    application = MagicMock()
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.stop = AsyncMock()
    application.shutdown = AsyncMock()
    application.post_init = AsyncMock()
    application.post_stop = None
    application.post_shutdown = None
    application.running = True
    application.updater = MagicMock()
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock()
    application.updater.running = True
    return application


# ── 1. /start in private chat works ──────────────────────────────────


class TestStartPrivateChat(unittest.IsolatedAsyncioTestCase):
    """/start runs the onboarding flow in private chats."""

    def setUp(self) -> None:
        bot_mod.db.init_db()
        bot_mod.db.register_user(_TEST_USER_ID, "tester", "Tester")
        bot_mod.db.set_user_language(_TEST_USER_ID, "ar")
        unlock_user(_TEST_USER_ID)

    async def test_start_private_chat_starts_onboarding(self) -> None:
        update = _make_update(_TEST_USER_ID, "/start", chat_type="private")
        ctx = _make_context()

        result = await bot_mod.start(update, ctx)

        self.assertEqual(result, bot_mod.ANTI_BOT)
        update.message.reply_text.assert_awaited_once()
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("بوت", reply)  # anti-bot challenge was sent
        self.assertIn("anti_bot_answer", ctx.user_data)

    async def test_start_private_chat_without_language_asks_language(
        self,
    ) -> None:
        # Fresh user without a persisted language → language prompt first.
        fresh_id = 555556
        bot_mod.db.register_user(fresh_id, "fresh", "Fresh")
        update = _make_update(fresh_id, "/start", chat_type="private")
        ctx = _make_context()

        result = await bot_mod.start(update, ctx)

        self.assertEqual(result, bot_mod.LANGUAGE_SELECT)
        update.message.reply_text.assert_awaited_once()


# ── 2. /start in group chat starts nothing ───────────────────────────


class TestStartGroupChat(unittest.IsolatedAsyncioTestCase):
    """/start must never start the private onboarding flow in groups."""

    def setUp(self) -> None:
        bot_mod.db.init_db()
        bot_mod.db.register_user(_TEST_USER_ID, "tester", "Tester")
        bot_mod.db.set_user_language(_TEST_USER_ID, "ar")
        unlock_user(_TEST_USER_ID)

    async def test_start_group_starts_no_onboarding(self) -> None:
        for chat_type in ("group", "supergroup", "channel"):
            update = _make_update(_TEST_USER_ID, "/start", chat_type=chat_type)
            ctx = _make_context()

            with patch.object(bot_mod.db, "register_user") as register:
                result = await bot_mod.start(update, ctx)

            self.assertEqual(
                result,
                ConversationHandler.END,
                f"/start in {chat_type} must not start onboarding",
            )
            update.message.reply_text.assert_not_awaited()
            register.assert_not_called()
            self.assertEqual(dict(ctx.user_data), {})


# ── 3. Anti-bot / subscription gates stay silent in groups ───────────


class TestGroupSilence(unittest.IsolatedAsyncioTestCase):
    """No unsolicited operational messages are ever sent to groups."""

    def setUp(self) -> None:
        CHANNELS.clear()
        CHANNELS["ch_iso"] = _required_channel()
        unlock_user(_TEST_USER_ID)

    def tearDown(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_subscription_message_gate_silent_in_group(self) -> None:
        lock_user(_TEST_USER_ID)
        update = _make_update(_TEST_USER_ID, "hello", chat_type="supergroup")
        bot = _make_bot()
        ctx = _make_context(bot)

        await bot_mod.subscription_message_gate(update, ctx)

        update.message.reply_text.assert_not_awaited()
        bot.get_chat_member.assert_not_awaited()

    async def test_subscription_message_gate_private_still_gates(self) -> None:
        # Required-channel behaviour for private users is preserved.
        lock_user(_TEST_USER_ID)
        member = MagicMock()
        member.status = "left"
        bot = _make_bot()
        bot.get_chat_member.return_value = member
        update = _make_update(_TEST_USER_ID, "hello", chat_type="private")
        ctx = _make_context(bot)

        await bot_mod.subscription_message_gate(update, ctx)

        bot.get_chat_member.assert_awaited_once()
        update.message.reply_text.assert_awaited_once()
        self.assertTrue(is_locked(_TEST_USER_ID))

    async def test_anti_bot_handlers_silent_in_group(self) -> None:
        ctx = _make_context()
        ctx.user_data["anti_bot_answer"] = 42
        update = _make_update(_TEST_USER_ID, "42", chat_type="group")

        result = await bot_mod.check_answer(update, ctx)

        self.assertEqual(result, ConversationHandler.END)
        update.message.reply_text.assert_not_awaited()
        # Early return: the anti-bot conversation state is untouched.
        self.assertIn("anti_bot_answer", ctx.user_data)

        blocked_update = _make_update(_TEST_USER_ID, "x", chat_type="group")
        blocked_result = await bot_mod._blocked(blocked_update, _make_context())
        self.assertEqual(blocked_result, ConversationHandler.END)
        blocked_update.message.reply_text.assert_not_awaited()

        lang_update = _make_update(_TEST_USER_ID, "text", chat_type="group")
        lang_result = await bot_mod.language_prompt_again(
            lang_update, _make_context()
        )
        self.assertEqual(lang_result, ConversationHandler.END)
        lang_update.message.reply_text.assert_not_awaited()

    async def test_verify_subscription_silent_in_group(self) -> None:
        update = _make_update(_TEST_USER_ID, chat_type="channel")
        ctx = _make_context(_make_bot())

        await bot_mod.verify_subscription(update, ctx)

        update.callback_query.answer.assert_not_awaited()
        update.callback_query.edit_message_text.assert_not_awaited()


# ── 4. Required channel/group remains silent ─────────────────────────


class TestRequiredChannelSilence(unittest.IsolatedAsyncioTestCase):
    """chat_member handling locks users without messaging any chat."""

    def setUp(self) -> None:
        CHANNELS.clear()
        CHANNELS["ch_iso"] = _required_channel()
        unlock_user(_TEST_USER_ID)

    def tearDown(self) -> None:
        CHANNELS.clear()
        unlock_user(_TEST_USER_ID)

    async def test_leave_locks_user_without_sending_messages(self) -> None:
        update = MagicMock()
        chat_member_update = MagicMock()
        chat_member_update.chat = MagicMock()
        chat_member_update.chat.id = _REQUIRED_CHANNEL_ID
        chat_member_update.new_chat_member = MagicMock()
        chat_member_update.new_chat_member.status = "left"
        chat_member_update.new_chat_member.user = MagicMock()
        chat_member_update.new_chat_member.user.id = _TEST_USER_ID
        update.chat_member = chat_member_update
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()

        ctx = _make_context()

        with patch("bot._REQUIRED_CHANNEL_IDS", {_REQUIRED_CHANNEL_ID}):
            await bot_mod.on_chat_member_update(update, ctx)

        self.assertTrue(is_locked(_TEST_USER_ID))
        # Nothing is ever sent into the required channel/group.
        ctx.bot.send_message.assert_not_called()
        ctx.bot.send_photo.assert_not_called()
        update.message.reply_text.assert_not_awaited()


# ── 5. Registration wiring (main) ────────────────────────────────────


class TestMainRegistrations(unittest.TestCase):
    """Handler registration as asserted from main()'s captured handlers."""

    def setUp(self) -> None:
        self._saved_channels = dict(CHANNELS)
        CHANNELS.clear()

    def tearDown(self) -> None:
        CHANNELS.clear()
        CHANNELS.update(self._saved_channels)

    def _capture_main_handlers(self) -> list:
        captured: list = []
        app = MagicMock()
        app.add_handler = lambda handler, group=None: captured.append(
            (handler, group)
        )
        builder = MagicMock()
        builder.token.return_value.build.return_value = app
        with patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "12345:TESTTOKEN"}
        ), patch.object(
            bot_mod, "ApplicationBuilder", return_value=builder
        ), patch.object(bot_mod, "run_single_entry"), patch.object(
            bot_mod, "db"
        ):
            CHANNELS.clear()  # zero configured channels at startup
            bot_mod.main()
        return captured

    def test_chat_member_handler_registered_with_zero_channels(self) -> None:
        self.assertEqual(len(CHANNELS), 0)
        captured = self._capture_main_handlers()

        handlers = [
            h for h, _group in captured if isinstance(h, ChatMemberHandler)
        ]
        self.assertEqual(
            len(handlers), 1,
            "ChatMemberHandler must be registered unconditionally",
        )
        self.assertIs(handlers[0].callback, bot_mod.on_chat_member_update)

    def test_start_entry_point_rejects_group_chats(self) -> None:
        captured = self._capture_main_handlers()
        conv = next(
            h for h, _group in captured if isinstance(h, ConversationHandler)
        )
        entry = conv.entry_points[0]
        self.assertIsInstance(entry, CommandHandler)

        group_update = _real_update("supergroup", "/start", command=True)
        private_update = _real_update("private", "/start", command=True)
        self.assertFalse(
            entry.check_update(group_update),
            "group /start must not match the onboarding entry point",
        )
        self.assertTrue(entry.check_update(private_update))

    def test_subscription_gate_registered_private_only(self) -> None:
        captured = self._capture_main_handlers()
        gate = next(
            h
            for h, _group in captured
            if isinstance(h, MessageHandler)
            and h.callback is bot_mod.subscription_message_gate
        )
        self.assertFalse(
            gate.check_update(_real_update("supergroup", "hello")),
            "the message gate must never run in group chats",
        )
        self.assertTrue(gate.check_update(_real_update("private", "hello")))

    def test_dead_subscription_gate_never_registered(self) -> None:
        source = inspect.getsource(bot_mod.main)
        self.assertNotIn(" subscription_gate", source)


# ── 6. allowed_updates includes chat_member ──────────────────────────


class TestAllowedUpdates(unittest.TestCase):
    """Polling must explicitly request chat_member updates."""

    def test_polling_requests_chat_member_updates(self) -> None:
        application = _make_application_mock()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=bot_mod._run_telegram_bot,
            args=(application, stop_event),
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while (
                application.updater.start_polling.await_count == 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertEqual(
                application.updater.start_polling.await_count, 1
            )
            kwargs = application.updater.start_polling.await_args.kwargs
            allowed = kwargs.get("allowed_updates")
            self.assertIsNotNone(
                allowed,
                "start_polling must receive allowed_updates explicitly",
            )
            self.assertIn("chat_member", allowed)
        finally:
            stop_event.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())


# ── 7. AdminNotifier targets only configured ADMINS ──────────────────


class TestAdminNotifierTargets(unittest.IsolatedAsyncioTestCase):
    """AdminNotifier may deliver only to config.ADMINS."""

    def setUp(self) -> None:
        self.sent: list[tuple[int, str]] = []

        async def _send(chat_id: int, text: str) -> None:
            self.sent.append((chat_id, text))

        self.notifier = AdminNotifier(_send)

    async def test_targets_only_configured_admins(self) -> None:
        self.assertTrue(ADMINS, "config.ADMINS must be configured")

        delivered = await self.notifier.notify(ADMINS[0], "ops ping")

        self.assertEqual(delivered, list(ADMINS))
        self.assertEqual([c for c, _body in self.sent], list(ADMINS))
        for chat_id, body in self.sent:
            self.assertIn(chat_id, ADMINS)
            self.assertEqual(body, "ops ping")
        self.assertEqual(list(self.notifier.admin_ids), list(ADMINS))

    async def test_non_admin_target_is_rejected(self) -> None:
        required_channel_id = -1001234567890
        with self.assertRaises(AdminNotifierTargetError):
            await self.notifier.notify(
                ADMINS[0], "x", targets=[required_channel_id]
            )
        self.assertEqual(self.sent, [])

    async def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(AdminNotifierError):
            await self.notifier.notify(ADMINS[0], "   ")
        self.assertEqual(self.sent, [])


# ── 8. Non-admin cannot invoke AdminNotifier ─────────────────────────


class TestAdminNotifierAuthorization(unittest.IsolatedAsyncioTestCase):
    """Only configured admins may drive the notifier."""

    def setUp(self) -> None:
        self.sent: list[tuple[int, str]] = []

        async def _send(chat_id: int, text: str) -> None:
            self.sent.append((chat_id, text))

        self.notifier = AdminNotifier(_send)

    async def test_non_admin_cannot_invoke_notifier(self) -> None:
        non_admin_id = 999999999
        self.assertFalse(is_admin(non_admin_id))

        with self.assertRaises(AdminNotifierAuthorizationError):
            await self.notifier.notify(non_admin_id, "should never send")

        self.assertEqual(self.sent, [])

    def test_authorization_errors_are_admin_notifier_errors(self) -> None:
        self.assertTrue(
            issubclass(AdminNotifierAuthorizationError, AdminNotifierError)
        )
        self.assertTrue(issubclass(AdminNotifierTargetError, AdminNotifierError))

    def test_admin_ids_property_matches_config(self) -> None:
        self.assertEqual(list(self.notifier.admin_ids), list(ADMINS))


# ── 9. Dead duplicate gate is disabled but importable ────────────────


class TestDeadSubscriptionGateDisabled(unittest.IsolatedAsyncioTestCase):
    """subscription_gate stays importable/callable but does nothing."""

    def test_symbol_remains_importable(self) -> None:
        from bot import subscription_gate

        self.assertTrue(callable(subscription_gate))

    async def test_gate_is_a_silent_no_op(self) -> None:
        update = _make_update(_TEST_USER_ID, "/listtasks", chat_type="private")
        bot = _make_bot()
        ctx = _make_context(bot)

        await bot_mod.subscription_gate(update, ctx)

        update.message.reply_text.assert_not_awaited()
        bot.get_chat_member.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
