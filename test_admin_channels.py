"""MT-ADMIN-CHANNELS — Channels module in the Admin Control Center.
===================================================================

The ``channels`` registry module DELEGATES to the existing
``/listchannels`` handler (``bot.list_channels``) through the
existing navigation shim — this file proves exactly that and
nothing more:

  A. Registry        — channels exists exactly once with the exact
                       label/description/command (A1-A4).
  B. Navigation      — ``ctl:channels`` parses, delegates through the
                       existing shim to ``bot.list_channels`` with the
                       REAL actor identity + private chat (A5-A9).
  C. Authorization   — the Control Center gate still refuses non-admins;
                       the target keeps its OWN auth + private guard;
                       no target run happens before authorization (A10-A12).
  D. Isolation       — private admin delegates; private non-admin refused;
                       group/channel silent for BOTH roles (A13-A18).
  E. Payload         — only the fixed ``ctl:channels`` token; no detail
                       grammar, no ids/slugs/usernames, foreign
                       namespaces still rejected (A19-A22).
  F. Registration    — /control, ^ctl: and /listchannels stay registered
                       exactly once; no second channels callback family
                       exists (A23-A25).
  G. No duplication  — admin_control issues NO SQL, NO channel store
                       calls and holds NO second channel implementation
                       (A26-A28).
  H. Safety          — opening + pressing performs no transaction and
                       leaves wallets/ledger/withdrawals/deposits/
                       payment methods/rate/tasks/users/admin_users/
                       support/required_channels byte-identical (A29).
  I. Existing flows  — /listchannels, the add-channel conversation entry
                       and the remove-channel flow behave exactly as
                       before (A30-A32).

Temp databases only (the established MT-ADMIN fixture).

Run:
    python3 -m pytest test_admin_channels.py -v
"""

from __future__ import annotations

import inspect
import sqlite3
import unittest
from unittest import mock

from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import bot as bot_mod
import db
from config import CHANNELS, Channel

from admin_control import (
    BACK_HINT,
    MSG_ADMIN_ONLY,
    build_dashboard_keyboard,
    parse_callback,
)
from test_admin_control import ControlTestBase, FINANCIAL_TABLES
from test_payment_methods import _answered, _callback, _run, _update
from test_admin_users import _capture_handlers
from test_withdrawal_service import ADMIN_ID

STRANGER = 999_999

_CHANNEL = Channel(
    slug="ch_main",
    channel_id=-100444,
    username="ch_main_user",
    title="Main Channel",
    required=True,
    chat_type="channel",
)

# Every table the channels press must leave byte-identical — the
# financial set plus admin-role, Support and channel persistence.
MUTATION_SAFETY_TABLES = FINANCIAL_TABLES + (
    "admin_users",
    "support_inquiries",
    "support_messages",
    "support_user_states",
    "support_reply_contexts",
    "required_channels",
)


class ChannelsTestBase(ControlTestBase):
    """Control fixture + isolated (empty) CHANNELS registry."""

    def setUp(self) -> None:
        super().setUp()
        saved = dict(CHANNELS)

        def restore() -> None:
            CHANNELS.clear()
            CHANNELS.update(saved)

        self.addCleanup(restore)
        CHANNELS.clear()

    # ── helpers ──────────────────────────────────────────────────

    def _seed_channel(self) -> Channel:
        CHANNELS[_CHANNEL.slug] = _CHANNEL
        db.save_channel(_CHANNEL)
        return _CHANNEL

    def _ctx(self, member_status: str = "member"):
        ctx = mock.MagicMock()
        ctx.user_data = {}
        ctx.bot.get_chat_member = mock.AsyncMock(
            return_value=mock.MagicMock(status=member_status)
        )
        return ctx

    def _dump_extra(self) -> dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return {
                table: [
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1"
                    )
                ]
                for table in MUTATION_SAFETY_TABLES
            }
        finally:
            conn.close()


# ── A. Registry ──────────────────────────────────────────────────────


class TestRegistry(ChannelsTestBase):
    """A1-A4: exactly one frozen ``channels`` entry with exact fields."""

    def test_01_channels_registered_exactly_once(self) -> None:
        keys = [m.key for m in admin_control.MODULES]
        self.assertEqual(keys.count("channels"), 1)
        self.assertIn("channels", admin_control.MODULES_BY_KEY)
        self.assertIn("channels", admin_control._KNOWN_OPS)

    def test_02_channels_label(self) -> None:
        self.assertEqual(
            admin_control.MODULES_BY_KEY["channels"].label, "📡 القنوات"
        )

    def test_03_channels_description(self) -> None:
        self.assertEqual(
            admin_control.MODULES_BY_KEY["channels"].description,
            "إدارة القنوات المطلوبة",
        )

    def test_04_channels_command_is_listchannels(self) -> None:
        module = admin_control.MODULES_BY_KEY["channels"]
        self.assertEqual(module.command, "/listchannels")
        payloads = self._buttons(build_dashboard_keyboard())
        self.assertEqual(payloads.count("ctl:channels"), 1)
        labels = [
            b.text
            for row in build_dashboard_keyboard().inline_keyboard
            for b in row
        ]
        self.assertIn(module.label, labels)


# ── B. Navigation ────────────────────────────────────────────────────


class TestNavigation(ChannelsTestBase):
    """A5-A9: fixed token → existing shim → bot.list_channels."""

    def test_05_ctl_channels_parses(self) -> None:
        self.assertEqual(parse_callback("ctl:channels"), "channels")

    def test_06_delegates_through_existing_navigation_shim(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:channels")

        target.assert_awaited_once()
        shim = target.await_args[0][0]
        self.assertEqual(shim.message.text, "/listchannels")
        update.callback_query.answer.assert_awaited_with(text=BACK_HINT)

    def test_07_target_is_bot_list_channels(self) -> None:
        # Static: the navigator references exactly the existing handler.
        source = inspect.getsource(admin_control._open_channels)
        self.assertIn("bot.list_channels", source)
        # Dynamic: pressing routes into that exact callable.
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:channels")
        target.assert_awaited_once()

    def test_08_target_receives_real_actor_identity(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:channels")

        shim = target.await_args[0][0]
        self.assertEqual(shim.effective_user.id, ADMIN_ID)

    def test_09_target_receives_real_private_chat(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:channels")

        shim = target.await_args[0][0]
        self.assertEqual(shim.effective_chat.type, "private")
        self.assertEqual(shim.effective_chat.id, ADMIN_ID)
        self.assertIsNotNone(shim.message.reply_text)


# ── C. Authorization ─────────────────────────────────────────────────


class TestAuthorization(ChannelsTestBase):
    """A10-A12: the control gate refuses; the target's own auth holds."""

    def test_10_non_admin_cannot_use_control_route(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:channels", actor_id=STRANGER)

        target.assert_not_awaited()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_11_target_own_authorization_remains_intact(self) -> None:
        self._seed_channel()

        # private admin → the existing listing renders.
        admin_update = _update(ADMIN_ID, "/listchannels", chat_type="private")
        _run(bot_mod.list_channels(admin_update, self._ctx()))
        admin_reply = admin_update.message.reply_text.call_args[0][0]
        self.assertIn("قنوات الاشتراك", admin_reply)
        self.assertIn("Main Channel", admin_reply)

        # private non-admin → the target's OWN admin-only refusal,
        # never the channel listing.
        user_update = _update(STRANGER, "/listchannels", chat_type="private")
        _run(bot_mod.list_channels(user_update, self._ctx()))
        user_reply = user_update.message.reply_text.call_args[0][0]
        self.assertIn("للمشرفين فقط", user_reply)
        self.assertNotIn("Main Channel", user_reply)

        # group → the target's own private-chat guard keeps it silent.
        group_update = _update(
            ADMIN_ID, "/listchannels", chat_type="supergroup"
        )
        _run(bot_mod.list_channels(group_update, self._ctx()))
        group_update.message.reply_text.assert_not_called()

    def test_12_no_target_run_before_authorization(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            # Non-admin never reaches the target.
            self._press("ctl:channels", actor_id=STRANGER)
            target.assert_not_called()

            # Group/channel never reaches the target.
            self._press("ctl:channels", chat_type="group")
            target.assert_not_called()

            # Private admin delegates.
            self._press("ctl:channels")
            target.assert_awaited_once()


# ── D. Isolation ─────────────────────────────────────────────────────


class TestIsolation(ChannelsTestBase):
    """A13-A18: private admin delegates; everything else stays silent."""

    def _press_channels_silent(self, actor_id: int, chat_type: str) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            update = self._press(
                "ctl:channels", actor_id=actor_id, chat_type=chat_type
            )
        target.assert_not_awaited()
        # No reply, no edit — an empty callback answer at most.
        self.assertIsNone(_answered(update.callback_query))
        update.callback_query.edit_message_text.assert_not_called()
        if update.callback_query.message is not None:
            update.callback_query.message.reply_text.assert_not_called()

    def test_13_admin_private_chat_delegates(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:channels", actor_id=ADMIN_ID)

        target.assert_awaited_once()
        self.assertEqual(_answered(update.callback_query), BACK_HINT)

    def test_14_non_admin_private_chat_refused(self) -> None:
        with mock.patch.object(
            bot_mod, "list_channels", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:channels", actor_id=STRANGER)

        target.assert_not_awaited()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_15_admin_group_chat_silent(self) -> None:
        self._press_channels_silent(ADMIN_ID, "group")

    def test_16_non_admin_group_chat_silent(self) -> None:
        self._press_channels_silent(STRANGER, "group")

    def test_17_admin_channel_chat_silent(self) -> None:
        self._press_channels_silent(ADMIN_ID, "channel")

    def test_18_non_admin_channel_chat_silent(self) -> None:
        self._press_channels_silent(STRANGER, "channel")


# ── E. Payload safety ────────────────────────────────────────────────


class TestPayload(ChannelsTestBase):
    """A19-A22: one fixed token — no detail grammar, no ids/slugs."""

    def test_19_only_fixed_ctl_channels_accepted(self) -> None:
        self.assertEqual(parse_callback("ctl:channels"), "channels")

    def test_20_malformed_channel_callbacks_rejected(self) -> None:
        # No channels sub-grammar exists: detail/add/remove/index forms
        # (which would require slugs/ids or a new grammar) all fail.
        for bad in (
            "ctl:channels:v:1",
            "ctl:channels:add",
            "ctl:channels:remove",
            "ctl:channels:p:0",
            "ctl:channels:slug:ch_main",
            "ctl:channels:ch_main",
            "ctl:channels:123",
            "ctl:channels ",
        ):
            self.assertIsNone(parse_callback(bad), bad)

    def test_21_no_ids_slugs_usernames_in_payloads(self) -> None:
        # Every dashboard payload is the fixed registry key shape —
        # no channel id, slug or username can travel in any button.
        for payload in self._buttons(build_dashboard_keyboard()):
            self.assertRegex(payload, r"^ctl:[a-z]+$")
        # The parser holds no channels branch to smuggle data through.
        self.assertNotIn(
            "channels", inspect.getsource(admin_control.parse_callback)
        )

    def test_22_foreign_namespaces_still_rejected(self) -> None:
        for bad in (
            "rmch:ch_main",
            "rmch_yes:ch_main",
            "rmch_no",
            "admin_panel:add",
            "admin_panel:remove",
            "admin_panel:list",
            "sup:open:1",
            "supcat:billing",
            "wd:view:1",
            "dp:view:1",
            "pm:view:1",
            "mrview:1",
            "mproof:approve:1",
            "atw:family:1",
        ):
            self.assertIsNone(parse_callback(bad), bad)


# ── F. Registration ──────────────────────────────────────────────────


class TestRegistration(unittest.TestCase):
    """A23-A25: one /control, one ^ctl:, one /listchannels, no second
    channels callback family."""

    def test_23_control_registered_exactly_once(self) -> None:
        captured, _bot_mod = _capture_handlers()
        handlers = [
            (h, g)
            for h, g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "control" in h.commands
        ]
        self.assertEqual(len(handlers), 1, "/control must be registered once")
        handler, group = handlers[0]
        self.assertIs(handler.callback, admin_control.control_command)
        self.assertEqual(group, 0)

    def test_24_ctl_callback_registered_exactly_once(self) -> None:
        captured, _bot_mod = _capture_handlers()
        handlers = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and getattr(h, "pattern", None) is not None
            and h.pattern.pattern == r"^ctl:"
        ]
        self.assertEqual(
            len(handlers), 1, "ctl: must be registered exactly once"
        )
        handler, group = handlers[0]
        self.assertIs(handler.callback, admin_control.control_callback)
        self.assertEqual(group, 5)

    def test_25_listchannels_once_and_no_second_channel_callback(
        self,
    ) -> None:
        captured, bot_mod_local = _capture_handlers()

        # /listchannels still registered exactly once (unchanged).
        listcmds = [
            h
            for h, _g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "listchannels" in h.commands
        ]
        self.assertEqual(len(listcmds), 1)
        self.assertIs(listcmds[0].callback, bot_mod_local.list_channels)

        # No callback family for channels was introduced anywhere.
        channel_callbacks = [
            h
            for h, _g in captured
            if isinstance(h, CallbackQueryHandler)
            and getattr(h, "pattern", None) is not None
            and "channels" in h.pattern.pattern
        ]
        self.assertEqual(channel_callbacks, [])

        # The legacy namespaces stay singly registered in bot source:
        # exactly one ^rmch: handler group entry and one admin_panel
        # family registration.
        source = open(bot_mod_local.__file__, encoding="utf-8").read()
        self.assertEqual(source.count('r"^rmch:"'), 1)
        self.assertEqual(source.count('"^admin_panel:add$"'), 1)


# ── G. No duplication / no SQL ───────────────────────────────────────


class TestNoDuplication(unittest.TestCase):
    """A26-A28: delegation only — no SQL, no channel store calls, no
    second channel implementation in admin_control."""

    def setUp(self) -> None:
        self.source = inspect.getsource(admin_control)

    def test_26_no_sql_in_admin_control(self) -> None:
        for token in (
            "SELECT",
            "INSERT",
            "UPDATE ",
            "DELETE ",
            "db.transaction(",
        ):
            self.assertNotIn(token, self.source, token)

    def test_27_no_channel_store_calls_from_control(self) -> None:
        for token in (
            "db.save_channel",
            "db.delete_channel",
            "db.load_channels",
            "db.get_channel_from_db",
            "CHANNELS",
        ):
            self.assertNotIn(token, self.source, token)

    def test_28_no_second_channel_implementation(self) -> None:
        # The add/remove conversation and legacy namespaces are never
        # referenced or re-implemented here — delegation is to the
        # existing /listchannels handler only.
        for token in (
            "addchannel_start",
            "addchannel_username",
            "addchannel_title",
            "removechannel_start",
            "removechannel_select",
            "removechannel_confirm",
            "admin_panel:",
            "rmch",
        ):
            self.assertNotIn(token, self.source, token)
        self.assertEqual(self.source.count("bot.list_channels"), 1)


# ── H. Safety proof (§Safety) ────────────────────────────────────────


class TestMutationSafety(ChannelsTestBase):
    """Opening + pressing ctl:channels mutates nothing anywhere."""

    def test_29_open_and_press_perform_no_mutation(self) -> None:
        self._seed_channel()
        self._seed_tasks()
        before = self._dump_extra()

        with mock.patch.object(db, "transaction") as txn:
            self._cmd()  # open the dashboard (real reads)
            update = self._press("ctl:channels")  # real delegation

        txn.assert_not_called()
        # wallets / ledger / withdrawals / deposits / payment methods /
        # rate / tasks / users / admin_users / support / channels —
        # every table byte-identical.
        self.assertEqual(before, self._dump_extra())
        # The real target rendered the existing channel listing.
        reply = update.callback_query.message.reply_text
        self.assertTrue(reply.called)
        self.assertIn("Main Channel", reply.call_args[0][0])


# ── I. Existing channel flows unchanged ──────────────────────────────


class TestExistingFlowsUnchanged(ChannelsTestBase):
    """A30-A32: the pre-existing add/list/remove flows behave exactly
    as before this task."""

    def test_30_listchannels_flow_unchanged(self) -> None:
        self._seed_channel()
        update = _update(ADMIN_ID, "/listchannels", chat_type="private")

        _run(bot_mod.list_channels(update, self._ctx()))

        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("قنوات الاشتراك", reply)
        self.assertIn("Main Channel", reply)
        self.assertIn("ch_main", reply)
        self.assertIn("ch_main_user", reply)
        self.assertIn("ID: `-100444`", reply)
        self.assertIn("required: نعم", reply)

    def test_31_addchannel_conversation_entry_unchanged(self) -> None:
        update = _update(ADMIN_ID, "/addchannel", chat_type="private")
        ctx = self._ctx()

        result = _run(bot_mod.addchannel_start(update, ctx))

        self.assertEqual(result, bot_mod.ADDCHANNEL_USERNAME)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("أرسل Username", reply)

    def test_32_removechannel_flow_unchanged(self) -> None:
        self._seed_channel()
        update = _update(ADMIN_ID, "/removechannel", chat_type="private")

        result = _run(bot_mod.removechannel_start(update, self._ctx()))

        self.assertEqual(result, bot_mod.REMOVECHANNEL_SELECT)
        reply = update.message.reply_text.call_args[0][0]
        self.assertIn("اختر القناة", reply)
        # Nothing was deleted at the selection stage.
        self.assertIn("ch_main", CHANNELS)
        self.assertIsNotNone(db.get_channel_from_db("ch_main"))


if __name__ == "__main__":
    unittest.main()
