"""MT-ADMIN-NEXT — Support module in the Admin Control Center.
=============================================================

The ``support`` registry module DELEGATES to the existing
``/support`` surface (``support_service.support_command``) through
the existing navigation shim — this file proves exactly that and
nothing more:

  A. Registry        — support exists exactly once with the exact
                       label/description/command (A1-A4).
  B. Navigation      — ``ctl:support`` parses, delegates through the
                       existing shim to ``support_service.support_command``
                       with the REAL actor identity + private chat (A5-A9).
  C. Authorization   — the Control Center gate still refuses non-admins;
                       Support's OWN authorization/isolation remain intact;
                       no Support read happens before authorization (A10-A12).
  D. Isolation       — private admin delegates; private non-admin refused;
                       group/channel silent for BOTH roles (A13-A18).
  E. Payload         — only the fixed ``ctl:support`` token; no sub-grammar,
                       no free text, foreign namespaces still rejected (A19-A22).
  F. Registration    — /control and ^ctl: stay registered exactly once (A23-A24).
  G. No duplication  — admin_control never touches support_store, never
                       issues Support SQL and holds no second Support
                       implementation (A25-A27).
  H. Mutation safety — pressing ctl:support opens no transaction and
                       leaves every financial/support/admin row identical.

Temp databases only (the established MT-ADMIN fixture); no production
destinations are used.

Run:
    python3 -m pytest test_admin_support.py -v
"""

from __future__ import annotations

import inspect
import sqlite3
import unittest
from unittest import mock

from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import db
import support_service
import support_store

from admin_control import (
    BACK_HINT,
    MSG_ADMIN_ONLY,
    build_dashboard_keyboard,
    parse_callback,
)
from test_admin_control import ControlTestBase, FINANCIAL_TABLES
from test_payment_methods import _answered, _run, _update
from test_admin_users import _capture_handlers
from test_withdrawal_service import ADMIN_ID

STRANGER = 999_999

# Rows the Control Center support press must leave byte-identical —
# the financial set plus the admin-role and Support persistence tables.
MUTATION_SAFETY_TABLES = FINANCIAL_TABLES + (
    "admin_users",
    "support_inquiries",
    "support_messages",
    "support_user_states",
    "support_reply_contexts",
)


# ── A. Registry ──────────────────────────────────────────────────────


class TestRegistry(ControlTestBase):
    """A1-A4: exactly one frozen ``support`` entry with exact fields."""

    def test_01_support_registered_exactly_once(self) -> None:
        keys = [m.key for m in admin_control.MODULES]
        self.assertEqual(keys.count("support"), 1)
        self.assertIn("support", admin_control.MODULES_BY_KEY)
        self.assertIn("support", admin_control._KNOWN_OPS)

    def test_02_support_label(self) -> None:
        self.assertEqual(
            admin_control.MODULES_BY_KEY["support"].label, "🎧 الدعم"
        )

    def test_03_support_description(self) -> None:
        self.assertEqual(
            admin_control.MODULES_BY_KEY["support"].description,
            "إدارة طلبات الدعم",
        )

    def test_04_support_command_is_support(self) -> None:
        module = admin_control.MODULES_BY_KEY["support"]
        self.assertEqual(module.command, "/support")
        # The dashboard keyboard carries the fixed payload exactly once.
        payloads = self._buttons(build_dashboard_keyboard())
        self.assertEqual(payloads.count("ctl:support"), 1)
        self.assertIn(module.label, [
            b.text
            for row in build_dashboard_keyboard().inline_keyboard
            for b in row
        ])


# ── B. Navigation ────────────────────────────────────────────────────


class TestNavigation(ControlTestBase):
    """A5-A9: fixed token → existing shim → support_service entry."""

    def test_05_ctl_support_parses(self) -> None:
        self.assertEqual(parse_callback("ctl:support"), "support")

    def test_06_delegates_through_existing_navigation_shim(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:support")

        target.assert_awaited_once()
        shim = target.await_args[0][0]
        self.assertEqual(shim.message.text, "/support")
        update.callback_query.answer.assert_awaited_with(text=BACK_HINT)

    def test_07_target_is_support_service_support_command(self) -> None:
        # Static: the only Support entry point referenced is the
        # existing one — no second handler exists.
        source = inspect.getsource(admin_control)
        self.assertIn("support_service.support_command", source)
        self.assertIn(
            "support_service.support_command",
            inspect.getsource(admin_control._open_support),
        )
        # Dynamic: pressing routes into that exact callable.
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:support")
        target.assert_awaited_once()

    def test_08_target_receives_real_actor_identity(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:support")

        shim = target.await_args[0][0]
        self.assertEqual(shim.effective_user.id, ADMIN_ID)

    def test_09_target_receives_real_private_chat(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:support")

        shim = target.await_args[0][0]
        self.assertEqual(shim.effective_chat.type, "private")
        self.assertEqual(shim.effective_chat.id, ADMIN_ID)
        # The shim re-binds the REAL reply (never a fake transport).
        self.assertIsNotNone(shim.message.reply_text)


# ── C. Authorization ─────────────────────────────────────────────────


class TestAuthorization(ControlTestBase):
    """A10-A12: the control gate refuses; Support's own auth is intact."""

    def test_10_non_admin_cannot_use_control_route(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:support", actor_id=STRANGER)

        target.assert_not_awaited()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_11_support_own_authorization_remains_intact(self) -> None:
        # Support's own handler still decides queue-vs-user-flow on its
        # OWN is_admin check — the control route adds nothing to it.
        with mock.patch.object(
            support_store, "list_open_inquiries", return_value=[]
        ) as reads:
            # private admin → the queue read runs (Support's own auth).
            admin_update = _update(ADMIN_ID, "/support", chat_type="private")
            _run(support_service.support_command(
                admin_update, mock.MagicMock()
            ))
            self.assertEqual(reads.call_count, 1)
            self.assertTrue(admin_update.message.reply_text.called)

            # private non-admin → Support's OWN user flow, never the
            # admin queue read.
            user_update = _update(STRANGER, "/support", chat_type="private")
            _run(support_service.support_command(
                user_update, mock.MagicMock()
            ))
            self.assertEqual(reads.call_count, 1)

            # group → Support's own _non_private_chat keeps it silent.
            group_update = _update(
                ADMIN_ID, "/support", chat_type="supergroup"
            )
            _run(support_service.support_command(
                group_update, mock.MagicMock()
            ))
            group_update.message.reply_text.assert_not_called()
            self.assertEqual(reads.call_count, 1)

    def test_12_no_support_read_before_authorization(self) -> None:
        with mock.patch.object(
            support_store, "list_open_inquiries", return_value=[]
        ) as reads:
            # Non-admin never reaches the target.
            self._press("ctl:support", actor_id=STRANGER)
            reads.assert_not_called()

            # Group/channel never reaches the target.
            self._press("ctl:support", chat_type="group")
            reads.assert_not_called()

            # Private admin delegates and the read happens inside the
            # target, after every gate.
            self._press("ctl:support")
            self.assertEqual(reads.call_count, 1)


# ── D. Isolation ─────────────────────────────────────────────────────


class TestIsolation(ControlTestBase):
    """A13-A18: private admin delegates; everything else stays silent."""

    def _press_support_silent(self, actor_id: int, chat_type: str):
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target, mock.patch.object(
            support_store, "list_open_inquiries", return_value=[]
        ) as reads:
            update = self._press(
                "ctl:support", actor_id=actor_id, chat_type=chat_type
            )
        target.assert_not_awaited()
        reads.assert_not_called()
        # No reply, no edit — an empty callback answer at most.
        self.assertIsNone(_answered(update.callback_query))
        update.callback_query.edit_message_text.assert_not_called()
        if update.callback_query.message is not None:
            update.callback_query.message.reply_text.assert_not_called()
        return update

    def test_13_admin_private_chat_delegates(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:support", actor_id=ADMIN_ID)

        target.assert_awaited_once()
        self.assertEqual(_answered(update.callback_query), BACK_HINT)

    def test_14_non_admin_private_chat_refused(self) -> None:
        with mock.patch.object(
            support_service, "support_command", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:support", actor_id=STRANGER)

        target.assert_not_awaited()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_15_admin_group_chat_silent(self) -> None:
        self._press_support_silent(ADMIN_ID, "group")

    def test_16_non_admin_group_chat_silent(self) -> None:
        self._press_support_silent(STRANGER, "group")

    def test_17_admin_channel_chat_silent(self) -> None:
        self._press_support_silent(ADMIN_ID, "channel")

    def test_18_non_admin_channel_chat_silent(self) -> None:
        self._press_support_silent(STRANGER, "channel")


# ── E. Payload safety ────────────────────────────────────────────────


class TestPayload(ControlTestBase):
    """A19-A22: one fixed token; no ids, no free text, no foreign family."""

    def test_19_only_fixed_ctl_support_accepted(self) -> None:
        self.assertEqual(parse_callback("ctl:support"), "support")

    def test_20_sub_grammar_rejected(self) -> None:
        # No ctl:support:… grammar exists (the existing parser only
        # knows the fixed registry key for this module).
        for bad in (
            "ctl:support:123",
            "ctl:support:new",
            "ctl:support:open",
            "ctl:support ",
        ):
            self.assertIsNone(parse_callback(bad), bad)

    def test_21_free_text_payloads_rejected(self) -> None:
        for bad in (
            "ctl:SUPPORT",
            "ctl:Support",
            "ctl: support",
            "ctl:support\n",
            "ctl:",
            "support",
            None,
            5,
        ):
            self.assertIsNone(parse_callback(bad), repr(bad))

    def test_22_foreign_namespaces_still_rejected(self) -> None:
        for bad in (
            "sup:open:1",
            "supcat:billing",
            "wd:view:1",
            "dp:view:1",
            "pm:view:1",
            "mrview:1",
            "mproof:approve:1",
            "atw:family:1",
            "rmch:slug",
            "admin_panel:list",
        ):
            self.assertIsNone(parse_callback(bad), bad)


# ── F. Registration ──────────────────────────────────────────────────


class TestRegistration(unittest.TestCase):
    """A23-A24: no second /control or ^ctl: registration."""

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


# ── G. No duplication ────────────────────────────────────────────────


class TestNoDuplication(unittest.TestCase):
    """A25-A27: delegation only — no Support store/SQL/second copy."""

    def setUp(self) -> None:
        self.source = inspect.getsource(admin_control)

    def test_25_admin_control_never_references_support_store(self) -> None:
        self.assertNotIn("support_store", self.source)
        self.assertNotIn("list_open_inquiries", self.source)
        self.assertNotIn("get_active_inquiry", self.source)

    def test_26_no_support_database_access_in_admin_control(self) -> None:
        # No Support table names, no SQL, no transaction call sites.
        for token in (
            "support_inquiries",
            "support_messages",
            "support_user_states",
            "support_reply_contexts",
            "SELECT",
            "INSERT",
            "UPDATE support",
            "db.transaction(",
        ):
            self.assertNotIn(token, self.source, token)

    def test_27_no_second_support_implementation(self) -> None:
        # None of the Support write/read business logic is re-implemented
        # here; the single reference is the existing command handler.
        for token in (
            "submit_user_message",
            "append_admin_message",
            "close_inquiry",
            "mark_message_delivered",
            "build_queue_page",
            "notify_admins",
        ):
            self.assertNotIn(token, self.source, token)
        self.assertEqual(self.source.count("support_service"), 2)
        self.assertIn("support_service.support_command", self.source)


# ── H. Mutation safety (§13) ─────────────────────────────────────────


class TestMutationSafety(ControlTestBase):
    """Pressing ctl:support mutates nothing anywhere."""

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

    def test_31_press_support_opens_no_transaction_and_mutates_nothing(
        self,
    ) -> None:
        # Seed a little state through the existing production helpers,
        # then press the REAL delegated path.
        self._seed_tasks()
        before = self._dump_extra()

        with mock.patch.object(db, "transaction") as txn:
            update = self._press("ctl:support")

        txn.assert_not_called()
        self.assertEqual(before, self._dump_extra())
        # The real target answered with the existing Support queue view.
        reply = update.callback_query.message.reply_text
        self.assertTrue(reply.called)


if __name__ == "__main__":
    unittest.main()
