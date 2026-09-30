"""
Focused tests — Admin Broadcast System (MT-ADMIN-38)
====================================================

The ``broadcast`` module of the Admin Control Center: a persistent,
confirmation-gated, individually-delivered broadcast surface built on
the authoritative ``users`` table and the additive ``broadcasts``
store.  There is no process-global broadcast state, no second
authorization model (every entry re-checks ``config.is_admin``) and
no financial/task/admin-role mutation anywhere on this path.

Coverage required by MT-ADMIN-38 §TESTS (brief number → test name):

 A. AUTHORIZATION (1-5)
   1   → test_01   non-admin cannot open broadcast (before any read)
   2   → test_02   non-admin callback rejected (no state, no send)
   3   → test_03   group chat is silent
   4   → test_04   channel chat is silent
   5   → test_05   authorization occurs before user/state reads
 B. REGISTRY (6-8)
   6   → test_06   broadcast registered exactly once
   7   → test_07   ``ctl:`` registration remains exactly once
   8   → test_08   broadcast module is no longer a placeholder
 C. DASHBOARD (9-10)
   9   → test_09   count comes from the authoritative users store
   10  → test_10   count failure degrades safely
 D. COMPOSITION (11-16)
   11  → test_11   new broadcast opens (persisted draft armed)
   12  → test_12   empty message rejected (no broadcast job)
   13  → test_13   whitespace-only rejected
   14  → test_14   oversized message rejected; draft kept for retry
   15  → test_15   valid message creates the confirmation card
   16  → test_16   message body never placed in callback data
 E. CONFIRMATION (17-20)
   17  → test_17   confirmation shows aggregate count only
   18  → test_18   cancel performs no send, no user mutation
   19  → test_19   successful confirm starts exactly one broadcast
   20  → test_20   second confirm cannot resend
 F. DELIVERY (21-25)
   21  → test_21   successful recipients counted
   22  → test_22   blocked user counted as failure
   23  → test_23   unavailable user counted as failure
   24  → test_24   one failure does not abort remaining users
   25  → test_25   invariant success + failure == recipients
 G. CONCURRENCY (26-27)
   26  → test_26   concurrent confirm → exactly one sender wins
   27  → test_27   losing confirmation performs zero delivery
 H. PERSISTENCE (28-30)
   28  → test_28   state survives handler recreation (SQLite only)
   29  → test_29   stale callbacks are safe
   30  → test_30   completed broadcast cannot be replayed
 I. PRIVACY (31-33)
   31  → test_31   recipient identities are never rendered
   32  → test_32   message body is never logged
   33  → test_33   no destination/chat list is exposed
 J. FINANCIAL ISOLATION (34-40)
   34  → test_34   wallet not called
   35  → test_35   ledger not called / not mutated
   36  → test_36   withdrawal not called
   37  → test_37   deposit not called
   38  → test_38   rate mutation not called
   39  → test_39   payment-method mutation not called
   40  → test_40   task/reward mutation not called
 K. REGRESSION (41-45)
   41  → test_41   existing /control modules remain functional
   42  → test_42   users module remains functional
   43  → test_43   tasks module remains functional
   44  → test_44   admins module remains functional
   45  → test_45   foreign namespaces (wd/dp/pm/mr/atw/mproof/sup)
                   remain untouched, ``^ctl:`` still exactly once
 L. EXTRAS (46+)  static group-7 registration, unknown-population
                   guard, admin/user-table isolation, structural
                   security guards, secrets in output/logs

Temp databases only; no production destinations or balances used.

Run:
    .venv/bin/python -m pytest test_admin_broadcast.py -v
"""

from __future__ import annotations

import re
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)

import admin_control
import config
import db
import withdrawal_store

from admin_control import (
    ADMINS_PANEL_HEADER,
    BROADCAST_COMPOSE_HEADER,
    BROADCAST_CONFIRM_HEADER,
    BROADCAST_HEADER,
    BROADCAST_RESULT_HEADER,
    BROADCAST_SENDING_TEXT,
    HEADER,
    MSG_ADMIN_ONLY,
    MSG_BROADCAST_ALREADY,
    MSG_BROADCAST_EMPTY,
    MSG_BROADCAST_TOO_LONG,
    MSG_INVALID,
    MSG_MODULE_UNAVAILABLE,
    MSG_NO_PENDING,
    TASKS_PANEL_HEADER,
    TOAST_BROADCAST_CANCELLED,
    USERS_PANEL_HEADER,
    build_dashboard_keyboard,
    parse_callback,
)

from test_payment_methods import (
    _answered,
    _callback,
    _edited,
    _reply,
    _run,
    _update,
)
from test_admin_admins import _FakeApplication
from test_admin_control import (
    FINANCIAL_TABLES,
    MUTATION_SPY_TARGETS,
    ControlTestBase,
)
from test_admin_users import _capture_handlers
from test_withdrawal_service import ADMIN_ID, FUND, PM_DESTINATION, USER_DEST

STRANGER = 999_999
NA = "غير متاح"

# Unique message body — must never reach callback data or logs.
BODY = "نص الرسالة الترويجية 48291-marker"

# Recipient ids far from any aggregate count used in these tests.
RECIPIENTS = (771001, 771002)
BIG_RECIPIENTS = (772001, 772002, 772003)

# Bot-token shape — must never appear in any rendered output.
_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_FORBIDDEN_OUTPUT = (
    "secret", "password", "token", "initdata", "init_data",
    "task_coin", "rpc", "/home/", ".db",
)


class BroadcastTestBase(ControlTestBase):
    """MT-ADMIN-38 fixture: control-center drivers + broadcast
    drivers.  Broadcast state lives ONLY in SQLite — there is no
    module-level pending dict to clear between tests."""

    # ── drivers ────────────────────────────────────────────────

    def _press(
        self,
        data,
        actor_id: int = ADMIN_ID,
        chat_type: str = "private",
        *,
        message_gone: bool = False,
        answer_side_effect=None,
        context=None,
    ):
        update = _callback(actor_id, data, chat_type=chat_type)
        if message_gone:
            update.callback_query.message = None
        else:
            update.callback_query.message.reply_text = mock.AsyncMock()
        if answer_side_effect is not None:
            update.callback_query.answer = mock.AsyncMock(
                side_effect=answer_side_effect
            )
        _run(
            admin_control.control_callback(
                update,
                mock.MagicMock() if context is None else context,
            )
        )
        return update

    def _view(self, data, **kwargs):
        """Press *data* and return (update, edited text, markup)."""
        update = self._press(data, **kwargs)
        edit = update.callback_query.edit_message_text
        text = edit.call_args[0][0]
        markup = edit.call_args[1].get("reply_markup")
        return update, text, markup

    def _labels(self, markup) -> list[str]:
        return [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]

    def _payloads(self, markup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    def _ctx(self, send_side_effect=None):
        """A handler context whose bot records individual sends."""
        context = mock.MagicMock()
        context.bot.send_message = mock.AsyncMock(
            side_effect=send_side_effect
        )
        return context

    def _send_text(self, text, actor_id: int = ADMIN_ID,
                   chat_type: str = "private"):
        update = _update(actor_id, text, chat_type=chat_type)
        _run(admin_control.broadcast_text_input(update, mock.MagicMock()))
        return update

    # ── seeds ──────────────────────────────────────────────────

    def _seed_users(self, *ids: int) -> None:
        for uid in ids:
            self.assertTrue(db.register_user(uid, f"u{uid}", f"U{uid}"))

    # ── broadcast helpers ──────────────────────────────────────

    def _compose(self, body: str = BODY, users=RECIPIENTS):
        """Seed recipients, arm the draft, deliver the text.

        Returns (prompt_update, composed_update)."""
        self._seed_users(*users)
        prompt = self._press("ctl:broadcast:new")
        composed = self._send_text(body)
        return prompt, composed

    def _rows(self) -> list[dict]:
        with db.get_connection(self.db_path) as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM broadcasts ORDER BY id"
                )
            ]

    def _spies_for(self, *module_names: str) -> dict:
        """Patch the MUTATION_SPY_TARGETS belonging to *module_names*."""
        spies = {}
        for module, name in MUTATION_SPY_TARGETS:
            if module.__name__ in module_names:
                patcher = mock.patch.object(module, name)
                spies[f"{module.__name__}.{name}"] = patcher.start()
                self.addCleanup(patcher.stop)
        return spies

    @staticmethod
    def _assert_no_secrets(testcase, text: str) -> None:
        lowered = text.lower()
        for needle in _FORBIDDEN_OUTPUT:
            testcase.assertNotIn(needle, lowered, f"secret leak: {needle}")
        testcase.assertIsNone(_TOKEN_RE.search(text), "bot-token shape")


# ════════════════════════════════════════════════════════════════
# A. AUTHORIZATION (1-5)
# ════════════════════════════════════════════════════════════════


class TestBroadcastAuth(BroadcastTestBase):

    def test_01_non_admin_cannot_open_broadcast(self) -> None:
        """1. Non-admins get the standard refusal BEFORE any
        broadcast state or user count is read."""
        with mock.patch.object(db, "count_users") as count_spy, \
                mock.patch.object(db, "get_open_broadcast") as state_spy:
            update = self._press("ctl:broadcast", actor_id=STRANGER)
        count_spy.assert_not_called()
        state_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

    def test_02_non_admin_callback_rejected(self) -> None:
        """2. Every broadcast sub-op re-checks authorization: no
        state read, no render, no Telegram send."""
        self._compose()
        context = self._ctx()
        with mock.patch.object(db, "get_open_broadcast") as state_spy:
            for op in ("new", "confirm", "cancel"):
                update = self._press(
                    f"ctl:broadcast:{op}", actor_id=STRANGER,
                    context=context,
                )
                self.assertEqual(
                    _answered(update.callback_query), MSG_ADMIN_ONLY, op
                )
                update.callback_query.edit_message_text.assert_not_called()
        state_spy.assert_not_called()
        context.bot.send_message.assert_not_awaited()
        rows = self._rows()
        self.assertEqual(len(rows), 1)          # draft untouched
        self.assertEqual(rows[0]["message"], BODY)

    def test_03_group_chat_is_silent(self) -> None:
        """3. Group presses produce ZERO administrative responses."""
        update = self._press("ctl:broadcast", chat_type="supergroup")
        update.callback_query.edit_message_text.assert_not_called()
        self.assertIsNone(_answered(update.callback_query))

    def test_04_channel_chat_is_silent(self) -> None:
        """4. Channel presses produce ZERO administrative responses."""
        update = self._press("ctl:broadcast", chat_type="channel")
        update.callback_query.edit_message_text.assert_not_called()
        self.assertIsNone(_answered(update.callback_query))

    def test_05_authorization_before_user_and_state_reads(self) -> None:
        """5. Reads happen only AFTER config.is_admin — for both the
        callback path and the text-input path."""
        with mock.patch.object(db, "count_users") as count_spy, \
                mock.patch.object(db, "get_open_broadcast") as state_spy:
            self._press("ctl:broadcast", actor_id=STRANGER)
        count_spy.assert_not_called()

        # Text input: non-admin and group never even read broadcast
        # state, and nothing is validated or staged.
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")        # armed for the admin
        with mock.patch.object(db, "get_open_broadcast") as state_spy:
            update = self._send_text("hi there", actor_id=STRANGER)
            update_group = self._send_text("hi there", chat_type="supergroup")
        state_spy.assert_not_called()
        update.message.reply_text.assert_not_called()
        update_group.message.reply_text.assert_not_called()
        self.assertEqual(self._rows()[0]["message"], "")  # still armed


# ════════════════════════════════════════════════════════════════
# B. REGISTRY (6-8)
# ════════════════════════════════════════════════════════════════


class TestBroadcastRegistry(BroadcastTestBase):

    def test_06_broadcast_registered_exactly_once(self) -> None:
        """6. One registry entry, one dashboard payload, no second
        broadcast module."""
        keys = [m.key for m in admin_control.MODULES]
        self.assertEqual(keys.count("broadcast"), 1)
        self.assertIn("broadcast", admin_control.MODULES_BY_KEY)
        payloads = self._buttons(build_dashboard_keyboard())
        self.assertEqual(payloads.count("ctl:broadcast"), 1)

    def test_07_ctl_registration_exactly_once(self) -> None:
        """7. Still exactly one ``^ctl:`` registration; the broadcast
        text catch-all IS statically registered in bot.py (group 7)."""
        captured, bot_mod = _capture_handlers()
        ctl = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and getattr(h, "pattern", None) is not None
            and h.pattern.pattern == r"^ctl:"
        ]
        self.assertEqual(len(ctl), 1, "ctl: must stay registered once")
        self.assertEqual(ctl[0][1], 5)

        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertIn("broadcast_text_input", source)
        broadcast_text = [
            (h, g)
            for h, g in captured
            if isinstance(h, MessageHandler)
            and getattr(h, "callback", None)
            is admin_control.broadcast_text_input
        ]
        self.assertEqual(
            len(broadcast_text), 1,
            "the broadcast catch-all must be registered in bot.py",
        )
        self.assertEqual(broadcast_text[0][1], 7)
        # No second /control entry either.
        control_handlers = [
            (h, g)
            for h, g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "control" in h.commands
        ]
        self.assertEqual(len(control_handlers), 1)

    def test_08_no_longer_a_placeholder(self) -> None:
        """8. The slot renders the functional panel instead of the
        unavailable notice, and its metadata describes the real
        module."""
        update = self._press("ctl:broadcast")
        text = _edited(update.callback_query)
        self.assertIn(BROADCAST_HEADER, text)
        self.assertNotEqual(
            _answered(update.callback_query), MSG_MODULE_UNAVAILABLE
        )
        module = admin_control.MODULES_BY_KEY["broadcast"]
        self.assertEqual(module.label, "📢 الإرسال الجماعي")
        self.assertEqual(module.description, "إرسال رسالة للمستخدمين المسجلين")
        self.assertIsNone(module.command)   # rendered in place
        self.assertNotIn("قريباً", module.description)
        self.assertNotIn("broadcast", admin_control._NAVIGATORS)


# ════════════════════════════════════════════════════════════════
# C. DASHBOARD (9-10)
# ════════════════════════════════════════════════════════════════


class TestBroadcastDashboard(BroadcastTestBase):

    def test_09_count_from_authoritative_users_store(self) -> None:
        """9. The panel count is db.count_users — the ONE source."""
        self._seed_users(*RECIPIENTS)
        with mock.patch.object(db, "count_users", wraps=db.count_users) as spy:
            _u, text, markup = self._view("ctl:broadcast")
        spy.assert_called_once()
        self.assertIn(f"👥 المستخدمون المسجلون: {len(RECIPIENTS)}", text)
        self.assertIn(BROADCAST_HEADER, text)
        self.assertEqual(
            self._payloads(markup),
            ["ctl:broadcast:new", "ctl:refresh"],
        )

    def test_10_count_failure_degrades_safely(self) -> None:
        """10. A failing count read degrades to ``غير متاح`` — no
        crash, no fabricated number, no unavailable notice."""
        with mock.patch.object(
            db, "count_users", side_effect=RuntimeError("db down")
        ):
            _u, text, markup = self._view("ctl:broadcast")
        self.assertIn(f"👥 المستخدمون المسجلون: {NA}", text)
        self.assertEqual(
            self._payloads(markup),
            ["ctl:broadcast:new", "ctl:refresh"],
        )


# ════════════════════════════════════════════════════════════════
# D. COMPOSITION (11-16)
# ════════════════════════════════════════════════════════════════


class TestBroadcastComposition(BroadcastTestBase):

    def test_11_new_broadcast_opens(self) -> None:
        """11. 📢 رسالة جديدة arms ONE persisted draft (compose
        state in SQLite, never a process-global dict) and shows the
        safe prompt with a cancel button."""
        update = self._press("ctl:broadcast:new")
        text = _edited(update.callback_query)
        self.assertIn(BROADCAST_COMPOSE_HEADER, text)
        self.assertIn("أرسل الآن الرسالة", text)
        markup = update.callback_query.edit_message_text.call_args[1][
            "reply_markup"
        ]
        self.assertEqual(self._payloads(markup), ["ctl:broadcast:cancel"])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "draft")
        self.assertEqual(rows[0]["message"], "")
        self.assertEqual(rows[0]["admin_user_id"], ADMIN_ID)

    def test_12_empty_message_rejected(self) -> None:
        """12. Empty text creates NO broadcast job — the draft stays
        armed and a later valid text still works."""
        self._press("ctl:broadcast:new")
        update = self._send_text("")
        self.assertEqual(_reply(update), MSG_BROADCAST_EMPTY)
        self.assertEqual(self._rows()[0]["message"], "")
        retry = self._send_text(BODY)
        self.assertIn(BROADCAST_CONFIRM_HEADER, _reply(retry))
        self.assertEqual(self._rows()[0]["message"], BODY)

    def test_13_whitespace_only_rejected(self) -> None:
        """13. Whitespace-only text is rejected the same way."""
        self._press("ctl:broadcast:new")
        update = self._send_text("   \n\t   ")
        self.assertEqual(_reply(update), MSG_BROADCAST_EMPTY)
        self.assertEqual(self._rows()[0]["message"], "")

    def test_14_oversized_message_rejected_draft_kept(self) -> None:
        """14. Over Telegram's real limit → ``رسالة طويلة جدًا``, no
        store write, nothing sent, and the pending state is KEPT so
        the admin can retry shorter.  Never silently truncated."""
        self._press("ctl:broadcast:new")
        update = self._send_text("x" * (db.MAX_BROADCAST_MESSAGE_LEN + 1))
        self.assertEqual(_reply(update), MSG_BROADCAST_TOO_LONG)
        self.assertEqual(self._rows()[0]["message"], "")   # not stored
        retry = self._send_text("رسالة قصيرة")
        self.assertIn(BROADCAST_CONFIRM_HEADER, _reply(retry))
        self.assertEqual(self._rows()[0]["message"], "رسالة قصيرة")

    def test_15_valid_message_creates_confirmation(self) -> None:
        """15. A valid message persists on the draft and renders the
        confirmation card with the aggregate count, the reviewed
        message and the confirm/cancel buttons."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        update = self._send_text(BODY)
        text = _reply(update)
        self.assertIn(BROADCAST_CONFIRM_HEADER, text)
        self.assertIn(f"👥 المستلمون: {len(RECIPIENTS)}", text)
        self.assertIn(BODY, text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        self.assertEqual(
            self._payloads(markup),
            ["ctl:broadcast:confirm", "ctl:broadcast:cancel"],
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], BODY)
        self.assertEqual(rows[0]["status"], "draft")

    def test_16_message_body_never_in_callback_data(self) -> None:
        """16. Callback payloads are the closed broadcast grammar
        only — the body never rides in a payload, and body-bearing or
        malformed payloads fail the parser."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        composed = self._send_text(BODY)
        markup = composed.message.reply_text.call_args[1]["reply_markup"]
        payloads = self._payloads(markup)
        self.assertEqual(
            payloads, ["ctl:broadcast:confirm", "ctl:broadcast:cancel"]
        )
        for payload in payloads:
            self.assertNotIn(BODY, payload)
            self.assertIsNotNone(parse_callback(payload))
        for bad in (
            "ctl:broadcast:",
            "ctl:broadcast:x",
            "ctl:broadcast:501",
            "ctl:broadcast:confirm:501",
            "ctl:broadcast:confirm:000123",
            f"ctl:broadcast:{BODY}",
            "ctl:broadcast new",
        ):
            self.assertIsNone(parse_callback(bad), bad)
        # Canonical ops round-trip exactly once each.
        for data in (
            "ctl:broadcast",
            "ctl:broadcast:new",
            "ctl:broadcast:confirm",
            "ctl:broadcast:cancel",
        ):
            self.assertEqual(parse_callback(data), data[len("ctl:"):])
            self.assertIsNone(parse_callback(data.upper()))


# ════════════════════════════════════════════════════════════════
# E. CONFIRMATION (17-20)
# ════════════════════════════════════════════════════════════════


class TestBroadcastConfirmation(BroadcastTestBase):

    def test_17_confirmation_displays_aggregate_count_only(self) -> None:
        """17. The card shows the aggregate count + the message —
        never recipient identities."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        composed = self._send_text(BODY)
        text = _reply(composed)
        self.assertIn(BROADCAST_CONFIRM_HEADER, text)
        self.assertIn(f"👥 المستلمون: {len(RECIPIENTS)}", text)
        self.assertIn(BODY, text)
        for uid in RECIPIENTS:
            self.assertNotIn(str(uid), text)
            self.assertNotIn(f"u{uid}", text)
            self.assertNotIn(f"U{uid}", text)
        self.assertNotIn("@", text)

    def test_18_cancel_performs_no_send(self) -> None:
        """18. Cancel clears the pending draft with NO send and NO
        user/financial mutation; a repeat cancel is a safe no-op."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        before = self._dump_state()
        context = self._ctx()
        update = self._press("ctl:broadcast:cancel", context=context)
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(
            _answered(update.callback_query), TOAST_BROADCAST_CANCELLED
        )
        self.assertIn(BROADCAST_HEADER, _edited(update.callback_query))
        rows = self._rows()
        self.assertEqual(rows[0]["status"], "cancelled")
        self.assertEqual(rows[0]["success_count"], 0)
        self.assertEqual(rows[0]["failure_count"], 0)
        self.assertEqual(before, self._dump_state())   # users untouched
        # Nothing pending → second cancel is deterministic, no send.
        context2 = self._ctx()
        update2 = self._press("ctl:broadcast:cancel", context=context2)
        context2.bot.send_message.assert_not_awaited()
        self.assertEqual(_answered(update2.callback_query), MSG_NO_PENDING)

    def test_19_successful_confirm_starts_exactly_one_broadcast(self) -> None:
        """19. One confirm → individual sends to exactly the users
        table population, the aggregate result card, and the row
        stamped completed with S + F = N."""
        self._seed_users(*BIG_RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        context = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=context)
        self.assertEqual(context.bot.send_message.await_count, 3)
        sent_to = sorted(
            c.kwargs["chat_id"]
            for c in context.bot.send_message.await_args_list
        )
        self.assertEqual(sent_to, list(BIG_RECIPIENTS))  # users table ONLY
        for c in context.bot.send_message.await_args_list:
            self.assertEqual(c.kwargs["text"], BODY)
        text = _edited(update.callback_query)
        self.assertIn(BROADCAST_RESULT_HEADER, text)
        self.assertIn("👥 المستلمون: 3", text)
        self.assertIn("✅ تم الإرسال: 3", text)
        self.assertIn("❌ فشل الإرسال: 0", text)
        row = self._rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["recipient_count"], 3)
        self.assertEqual(row["success_count"], 3)
        self.assertEqual(row["failure_count"], 0)
        self.assertEqual(
            row["success_count"] + row["failure_count"],
            row["recipient_count"],
        )
        # Exactly two edits: one intermediate state, one final
        # aggregate — never one edit per recipient.
        edits = [
            c.args[0]
            for c in update.callback_query.edit_message_text.call_args_list
        ]
        self.assertEqual(edits[0], BROADCAST_SENDING_TEXT)
        self.assertEqual(edits[-1], text)
        self.assertEqual(len(edits), 2)

    def test_20_second_confirm_cannot_resend(self) -> None:
        """20. A repeated/delayed confirm after completion performs
        ZERO sends and answers the deterministic notice."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        first = self._ctx()
        self._press("ctl:broadcast:confirm", context=first)
        self.assertEqual(first.bot.send_message.await_count, 2)

        second = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=second)
        second.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update.callback_query), MSG_BROADCAST_ALREADY)
        edits = [
            c.args[0]
            for c in update.callback_query.edit_message_text.call_args_list
        ]
        self.assertNotIn(BROADCAST_SENDING_TEXT, edits)  # no send pass
        row = self._rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["success_count"], 2)   # unchanged


# ════════════════════════════════════════════════════════════════
# F. DELIVERY (21-25)
# ════════════════════════════════════════════════════════════════


class TestBroadcastDelivery(BroadcastTestBase):

    def _deliver(self, send_side_effect=None):
        """Full flow: 3 recipients → confirm with a recording bot."""
        self._seed_users(*BIG_RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        context = self._ctx(send_side_effect)
        update = self._press("ctl:broadcast:confirm", context=context)
        return update, context

    def test_21_successful_recipients_counted(self) -> None:
        """21. Every delivered message counts as success."""
        update, context = self._deliver()
        text = _edited(update.callback_query)
        self.assertIn("✅ تم الإرسال: 3", text)
        self.assertIn("❌ فشل الإرسال: 0", text)
        self.assertEqual(context.bot.send_message.await_count, 3)
        row = self._rows()[0]
        self.assertEqual(row["success_count"], 3)
        self.assertEqual(row["failure_count"], 0)

    def test_22_blocked_user_counted_as_failure(self) -> None:
        """22. A user who blocked the bot counts as FAILED delivery —
        never a claimed success."""
        update, context = self._deliver(
            [None, Forbidden("bot was blocked by the user"), None]
        )
        text = _edited(update.callback_query)
        self.assertIn("✅ تم الإرسال: 2", text)
        self.assertIn("❌ فشل الإرسال: 1", text)
        row = self._rows()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["success_count"], 2)
        self.assertEqual(row["failure_count"], 1)

    def test_23_unavailable_user_counted_as_failure(self) -> None:
        """23. Invalid chat + unexpected network error both count as
        failed delivery."""
        update, _context = self._deliver(
            [
                BadRequest("Bad Request: chat not found"),
                TelegramError("temporary network failure"),
                None,
            ]
        )
        text = _edited(update.callback_query)
        self.assertIn("✅ تم الإرسال: 1", text)
        self.assertIn("❌ فشل الإرسال: 2", text)
        row = self._rows()[0]
        self.assertEqual(row["success_count"], 1)
        self.assertEqual(row["failure_count"], 2)

    def test_24_one_failure_does_not_abort_remaining_users(self) -> None:
        """24. The FIRST recipient failing still lets every remaining
        recipient be attempted — the loop never aborts."""
        update, context = self._deliver(
            [Forbidden("blocked"), None, None]
        )
        self.assertEqual(context.bot.send_message.await_count, 3)
        text = _edited(update.callback_query)
        self.assertIn("✅ تم الإرسال: 2", text)
        self.assertIn("❌ فشل الإرسال: 1", text)
        # Every remaining recipient was still attempted with the
        # body verbatim.
        for c in context.bot.send_message.await_args_list:
            self.assertEqual(c.kwargs["text"], BODY)

    def test_25_invariant_success_plus_failure_equals_recipients(self) -> None:
        """25. S + F = N always — mixed outcomes included."""
        update, _context = self._deliver(
            [Forbidden("blocked"), None, BadRequest("chat not found")]
        )
        row = self._rows()[0]
        self.assertEqual(
            row["success_count"] + row["failure_count"],
            row["recipient_count"],
        )
        self.assertEqual(row["recipient_count"], len(BIG_RECIPIENTS))
        text = _edited(update.callback_query)
        self.assertIn(f"👥 المستلمون: {row['recipient_count']}", text)
        self.assertIn(f"✅ تم الإرسال: {row['success_count']}", text)
        self.assertIn(f"❌ فشل الإرسال: {row['failure_count']}", text)


# ════════════════════════════════════════════════════════════════
# G. CONCURRENCY (26-27)
# ════════════════════════════════════════════════════════════════


class TestBroadcastConcurrency(BroadcastTestBase):

    def test_26_concurrent_confirm_exactly_one_winner(self) -> None:
        """26. Two confirmations racing the atomic claim: the DB
        transition (not a button, not an app lock) admits EXACTLY one
        winner."""
        self._seed_users(*RECIPIENTS)
        bid = db.arm_broadcast_draft(ADMIN_ID)
        db.save_broadcast_draft_message(bid, BODY)

        barrier = threading.Barrier(2, timeout=15)
        results: list[bool] = []

        def racer() -> None:
            barrier.wait()
            results.append(db.claim_broadcast_sending(bid, len(RECIPIENTS)))

        threads = [threading.Thread(target=racer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sorted(results), [False, True])
        # The winner owns the pass; the row is honestly 'sending'.
        self.assertEqual(self._rows()[0]["status"], "sending")

    def test_27_losing_confirmation_performs_zero_delivery(self) -> None:
        """27. A losing/duplicate/raced confirm begins NO Telegram
        send and reports the deterministic already-processed notice."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)

        # Winner pass.
        winner = self._ctx()
        self._press("ctl:broadcast:confirm", context=winner)
        self.assertEqual(winner.bot.send_message.await_count, 2)

        # Sequential duplicate → zero delivery.
        loser = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=loser)
        loser.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update.callback_query), MSG_BROADCAST_ALREADY)

        # Externally pre-claimed draft (another press won the race
        # before this handler ran) → also zero delivery.
        bid = db.arm_broadcast_draft(ADMIN_ID)
        db.save_broadcast_draft_message(bid, BODY)
        self.assertTrue(db.claim_broadcast_sending(bid, len(RECIPIENTS)))
        raced = self._ctx()
        update2 = self._press("ctl:broadcast:confirm", context=raced)
        raced.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update2.callback_query), MSG_BROADCAST_ALREADY)


# ════════════════════════════════════════════════════════════════
# H. PERSISTENCE (28-30)
# ════════════════════════════════════════════════════════════════


class TestBroadcastPersistence(BroadcastTestBase):

    def test_28_state_survives_handler_recreation(self) -> None:
        """28. Broadcast state lives ONLY in SQLite: there is no
        in-memory broadcast dict, and a draft created with no
        in-process interaction is picked up by a freshly-created
        handler (restart semantics) and re-read after a brand-new
        connection."""
        source = open(admin_control.__file__, encoding="utf-8").read()
        self.assertNotIn("_PENDING_BROADCAST", source)
        pending = [
            name for name in dir(admin_control)
            if name.startswith("_PENDING") and "BROADCAST" in name.upper()
        ]
        self.assertEqual(pending, [])

        self._seed_users(*RECIPIENTS)
        # Compose state armed with NO prior press in this "process".
        bid = db.arm_broadcast_draft(ADMIN_ID)
        self.assertEqual(db.get_open_broadcast(ADMIN_ID)["message"], "")

        # A freshly-created text handler reads it back from SQLite.
        composed = self._send_text("بدائل")
        self.assertIn(BROADCAST_CONFIRM_HEADER, _reply(composed))
        self.assertIn("بدائل", _reply(composed))
        self.assertEqual(self._rows()[0]["message"], "بدائل")

        # A brand-new connection (restart survival) sees the state.
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT message, status FROM broadcasts WHERE id = ?",
                (bid,),
            ).fetchone()
        self.assertEqual(row["message"], "بدائل")
        self.assertEqual(row["status"], "draft")

        # …and the confirm path resolves it end-to-end.
        context = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=context)
        self.assertEqual(context.bot.send_message.await_count, 2)
        self.assertIn(BROADCAST_RESULT_HEADER, _edited(update.callback_query))

    def test_29_stale_callbacks_are_safe(self) -> None:
        """29. Stale/delayed confirms respond safely — never resend,
        never recreate, never expose internal state."""
        context = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=context)
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update.callback_query), MSG_NO_PENDING)

        # Still-composing draft (no message yet) → not confirmable.
        self._press("ctl:broadcast:new")
        update2 = self._press("ctl:broadcast:confirm")
        self.assertEqual(_edited(update2.callback_query), MSG_NO_PENDING)

        # Cancelled draft → same safe answer, still no send.
        self._press("ctl:broadcast:cancel")
        context3 = self._ctx()
        update3 = self._press("ctl:broadcast:confirm", context=context3)
        context3.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update3.callback_query), MSG_NO_PENDING)

        # Unknown broadcast ops die at the parser (MSG_INVALID).
        update4 = self._press("ctl:broadcast:nope")
        self.assertEqual(_answered(update4.callback_query), MSG_INVALID)

    def test_30_completed_broadcast_cannot_be_replayed(self) -> None:
        """30. After completion the same broadcast is terminal: the
        confirm replays nothing, cancel mutates nothing, and a NEW
        broadcast gets a NEW row — the old one stays untouched."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        first = self._ctx()
        self._press("ctl:broadcast:confirm", context=first)
        original = self._rows()[0]

        # Replay attempts.
        replay = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=replay)
        replay.bot.send_message.assert_not_awaited()
        self.assertEqual(_edited(update.callback_query), MSG_BROADCAST_ALREADY)
        cancel = self._ctx()
        update2 = self._press("ctl:broadcast:cancel", context=cancel)
        cancel.bot.send_message.assert_not_awaited()
        self.assertEqual(_answered(update2.callback_query), MSG_NO_PENDING)

        after = self._rows()[0]
        self.assertEqual(after, original)   # byte-identical terminal row

        # A new broadcast is a NEW draft — the completed row stays.
        self._press("ctl:broadcast:new")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[1]["status"], "draft")
        self.assertGreater(rows[1]["id"], rows[0]["id"])


# ════════════════════════════════════════════════════════════════
# I. PRIVACY (31-33)
# ════════════════════════════════════════════════════════════════


class TestBroadcastPrivacy(BroadcastTestBase):

    def _all_texts(self) -> list[str]:
        """Render every broadcast surface and collect its text."""
        texts = []
        _u, panel, _m = self._view("ctl:broadcast")
        texts.append(panel)
        prompt = self._press("ctl:broadcast:new")
        texts.append(_edited(prompt.callback_query))
        composed = self._send_text(BODY)
        texts.append(_reply(composed))
        context = self._ctx()
        result = self._press("ctl:broadcast:confirm", context=context)
        texts.append(_edited(result.callback_query))
        return texts

    def test_31_recipient_identities_never_rendered(self) -> None:
        """31. No recipient list, no usernames, no Telegram ids —
        aggregate counts only."""
        self._seed_users(*BIG_RECIPIENTS)
        for text in self._all_texts():
            for uid in BIG_RECIPIENTS:
                self.assertNotIn(str(uid), text)
                self.assertNotIn(f"u{uid}", text)
                self.assertNotIn(f"U{uid}", text)
            self.assertNotIn("@", text)

    def test_32_message_body_never_logged(self) -> None:
        """32. Logs carry admin_id / broadcast_id / action / counts
        — never the message body, never recipient identities."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        with self.assertLogs("admin_control", level="INFO") as logs:
            self._send_text(BODY)
            context = self._ctx([Forbidden("blocked"), None])
            self._press("ctl:broadcast:confirm", context=context)
        joined = "\n".join(logs.output)
        self.assertNotIn(BODY, joined)
        for uid in RECIPIENTS:
            self.assertNotIn(str(uid), joined)
            self.assertNotIn(f"u{uid}", joined)
        # Safe operational ids ARE present.
        self.assertIn(f"admin={ADMIN_ID}", joined)
        self.assertIn("Broadcast completed", joined)

    def test_33_no_destination_or_chat_list_exposed(self) -> None:
        """33. No destinations, balances or chat/recipient list can
        reach any broadcast surface."""
        self._seed_pending_withdrawal(501)   # real destination + balance
        self._seed_users(*BIG_RECIPIENTS)
        for text in self._all_texts():
            self.assertNotIn(USER_DEST, text)
            self.assertNotIn(PM_DESTINATION, text)
            self.assertNotIn(str(FUND), text)
            self.assertNotIn("chat_id", text)
            for uid in BIG_RECIPIENTS:
                self.assertNotIn(str(uid), text)


# ════════════════════════════════════════════════════════════════
# J. FINANCIAL ISOLATION (34-40)
# ════════════════════════════════════════════════════════════════


class TestBroadcastFinancialIsolation(BroadcastTestBase):
    """The full broadcast flow must never touch money, tasks or
    roles — proven with mutation spies + byte-identical dumps."""

    def _run_full_flow(self):
        """Arm → compose → confirm.  Tests seed recipients BEFORE
        taking their state dump (seeding mutates the users table)."""
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        context = self._ctx()
        update = self._press("ctl:broadcast:confirm", context=context)
        self.assertIn(BROADCAST_RESULT_HEADER, _edited(update.callback_query))
        return context

    def test_34_wallet_not_called(self) -> None:
        """34. No wallet primitive runs and wallet rows are
        byte-identical."""
        self._seed_pending_withdrawal(501)   # wallets + ledger exist
        self._seed_users(*RECIPIENTS)
        spies = self._spies_for("wallet")
        txn_patcher = mock.patch.object(db, "transaction")
        txn = txn_patcher.start()
        self.addCleanup(txn_patcher.stop)
        before = self._dump_state()
        self._run_full_flow()
        txn.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    def test_35_ledger_not_called(self) -> None:
        """35. No ledger service exists on this path; ledger rows are
        byte-identical."""
        self._seed_pending_withdrawal(501)
        self._seed_users(*RECIPIENTS)
        section = self._broadcast_section()
        self.assertNotIn("LedgerService", section)
        self.assertIsNone(re.search(r"\bledger\.\w+\s*\(", section))
        before = self._dump_state()
        self._run_full_flow()
        self.assertEqual(before, self._dump_state())

    def test_36_withdrawal_not_called(self) -> None:
        """36. The withdrawal repository is never even instantiated
        during a broadcast; withdrawal rows are byte-identical."""
        self._seed_pending_withdrawal(501)
        self._seed_users(*RECIPIENTS)
        with mock.patch.object(
            withdrawal_store, "SqliteWithdrawalRepository"
        ) as repo:
            before = self._dump_state()
            self._run_full_flow()
        repo.assert_not_called()
        self.assertEqual(before, self._dump_state())

    def test_37_deposit_not_called(self) -> None:
        """37. No deposit entry point runs; deposit tables are
        byte-identical."""
        self._seed_deposit_proof(501)
        self._seed_users(*RECIPIENTS)
        spies = self._spies_for(
            "deposit_store", "deposit_proof_store", "deposit_manual_review"
        )
        before = self._dump_state()
        self._run_full_flow()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    def test_38_rate_mutation_not_called(self) -> None:
        """38. rate_store.set_rate never runs; the rate row is
        byte-identical."""
        self._seed_rate()
        self._seed_users(*RECIPIENTS)
        spies = self._spies_for("rate_store")
        before = self._dump_state()
        self._run_full_flow()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    def test_39_payment_method_mutations_not_called(self) -> None:
        """39. No payment-method mutation entry point runs; the
        table is byte-identical."""
        self._seed_users(*RECIPIENTS)
        spies = self._spies_for("payment_method_store")
        self.assertTrue(spies)   # the four mutations are spied
        before = self._dump_state()
        self._run_full_flow()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    def test_40_task_and_reward_mutations_not_called(self) -> None:
        """40. No task create/enable/edit, no reward settlement, no
        task-review decision runs; task tables are byte-identical."""
        self._seed_tasks()
        self._seed_users(*RECIPIENTS)
        spies = self._spies_for("admin_review_queue", "manual_task")
        extra = {}
        for name in ("update_task", "create_task"):
            patcher = mock.patch.object(db, name)
            extra[f"db.{name}"] = patcher.start()
            self.addCleanup(patcher.stop)
        before = self._dump_state()
        self._run_full_flow()
        for label, spy in {**spies, **extra}.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    @staticmethod
    def _broadcast_section() -> str:
        source = open(admin_control.__file__, encoding="utf-8").read()
        marker = "# ── Broadcast module (MT-ADMIN-38)"
        return source[source.index(marker):]


# ════════════════════════════════════════════════════════════════
# K. REGRESSION (41-45)
# ════════════════════════════════════════════════════════════════


class TestBroadcastRegression(BroadcastTestBase):

    def test_41_control_modules_remain_functional(self) -> None:
        """41. /control + refresh still render the full dashboard."""
        update = self._cmd()
        self.assertIn(HEADER, _reply(update))
        refresh = self._press("ctl:refresh")
        self.assertIn(HEADER, _edited(refresh.callback_query))
        # The dashboard still offers every module exactly once.
        payloads = self._buttons(build_dashboard_keyboard())
        self.assertEqual(len(payloads), len(set(payloads)))
        self.assertEqual(payloads.count("ctl:broadcast"), 1)

    def test_42_users_module_remains_functional(self) -> None:
        """42. The users panel still renders in place."""
        _u, text, _m = self._view("ctl:users")
        self.assertIn(USERS_PANEL_HEADER, text)

    def test_43_tasks_module_remains_functional(self) -> None:
        """43. The tasks panel still renders in place."""
        _u, text, _m = self._view("ctl:tasks")
        self.assertIn(TASKS_PANEL_HEADER, text)

    def test_44_admins_module_remains_functional(self) -> None:
        """44. The admins panel still renders in place."""
        _u, text, _m = self._view("ctl:admins")
        self.assertIn(ADMINS_PANEL_HEADER, text)

    def test_45_foreign_namespaces_untouched(self) -> None:
        """45. wd:/dp:/pm:/mr:/atw:/mproof:/sup: stay registered
        exactly once, ``^ctl:`` stays exactly once, and the parser
        rejects every foreign payload."""
        import bot as bot_mod

        source = open(bot_mod.__file__, encoding="utf-8").read()
        for pattern in (
            'pattern=r"^wd:"',
            'pattern=r"^dp:"',
            'pattern=r"^pm:"',
            'pattern=r"^mr(view|vp):"',
            'pattern=r"^atw:"',
            'pattern=r"^mproof:"',
            'pattern=r"^sup:"',
            'pattern=r"^ctl:"',
        ):
            self.assertEqual(
                source.count(pattern), 1, f"{pattern} must stay unique"
            )
        for foreign in (
            "wd:list", "dp:open", "pm:list", "mr:view", "atw:start",
            "mproof:1", "sup:open",
        ):
            self.assertIsNone(parse_callback(foreign), foreign)
        ctl_source = open(admin_control.__file__, encoding="utf-8").read()
        for foreign in (
            'callback_data="wd', 'callback_data="dp', 'callback_data="pm',
            'callback_data="mr', 'callback_data="atw',
            'callback_data="mproof', 'callback_data="sup',
        ):
            self.assertNotIn(foreign, ctl_source)


# ════════════════════════════════════════════════════════════════
# L. EXTRAS (46+): static registration, guards, isolation, secrets
# ════════════════════════════════════════════════════════════════


class TestBroadcastRegistration(BroadcastTestBase):

    def test_46_no_handler_added_group_7_static(self) -> None:
        """46. Pressing ctl:broadcast (twice) adds ZERO handlers
        through context.application — the group-7 catch-all is
        already static in bot.py, and groups 3/6/7 all coexist there
        — one catch-all per group, so no admin text input is
        starved."""
        app = _FakeApplication()
        ctx = SimpleNamespace(application=app)
        for _ in range(2):
            update = _callback(ADMIN_ID, "ctl:broadcast")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
        self.assertEqual(app.added, [])

        # All three catch-alls — statically, one per group.
        captured, _bot = _capture_handlers()
        for callback, expected_group in (
            (admin_control.task_edit_text_input, 3),
            (admin_control.admin_add_text_input, 6),
            (admin_control.broadcast_text_input, 7),
        ):
            groups = [
                g
                for h, g in captured
                if isinstance(h, MessageHandler)
                and getattr(h, "callback", None) is callback
            ]
            self.assertEqual(groups, [expected_group])

    def test_47_context_without_live_application_is_noop(self) -> None:
        """47. Unit-test shims (no Application / MagicMock bot_data)
        degrade to a no-op — the view still renders."""
        for ctx in (SimpleNamespace(), mock.MagicMock()):
            update = _callback(ADMIN_ID, "ctl:broadcast")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
            update.callback_query.edit_message_text.assert_awaited_once()

    def test_48_unknown_population_never_sends(self) -> None:
        """48. When the authoritative count cannot be obtained the
        card degrades to ``غير متاح`` WITHOUT a confirm button, and a
        failing recipient enumeration at confirm time claims nothing,
        sends nothing, and stays retryable."""
        self._seed_users(*RECIPIENTS)
        self._press("ctl:broadcast:new")
        with mock.patch.object(
            db, "count_users", side_effect=RuntimeError("db down")
        ):
            composed = self._send_text(BODY)
        text = _reply(composed)
        self.assertIn(f"👥 المستلمون: {NA}", text)
        markup = composed.message.reply_text.call_args[1]["reply_markup"]
        self.assertEqual(self._payloads(markup), ["ctl:broadcast:cancel"])

        # Confirm-side guard: enumeration fails → no claim, no send.
        context = self._ctx()
        with mock.patch.object(
            db,
            "list_broadcast_recipient_ids",
            side_effect=ValueError("broadcast recipient population too large"),
        ):
            update = self._press("ctl:broadcast:confirm", context=context)
        context.bot.send_message.assert_not_awaited()
        text2 = _edited(update.callback_query)
        self.assertIn(f"👥 المستلمون: {NA}", text2)
        markup2 = update.callback_query.edit_message_text.call_args[1][
            "reply_markup"
        ]
        self.assertEqual(self._payloads(markup2), ["ctl:broadcast:cancel"])
        # The draft survives — the admin can retry once reads recover.
        self.assertEqual(self._rows()[0]["status"], "draft")


class TestBroadcastIsolationGuards(BroadcastTestBase):

    def test_49_admin_roles_and_users_untouched(self) -> None:
        """Broadcast never modifies admin_users, config.ADMINS or
        the users table — authorization stays exactly config.is_admin."""
        self._seed_pending_withdrawal(501)
        self._seed_users(*RECIPIENTS)
        before_admins = list(config.ADMINS)
        before = self._dump_state()
        self._run_confirm_flow()
        self.assertEqual(before, self._dump_state())
        self.assertEqual(list(config.ADMINS), before_admins)
        with db.get_connection(self.db_path) as conn:
            admin_rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT user_id, active, added_by FROM admin_users "
                    "ORDER BY user_id"
                )
            ]
        self.assertTrue(admin_rows)   # unchanged set still present

    def _run_confirm_flow(self) -> None:
        self._press("ctl:broadcast:new")
        self._send_text(BODY)
        context = self._ctx()
        self._press("ctl:broadcast:confirm", context=context)
        self.assertEqual(context.bot.send_message.await_count, 3)

    def test_50_structural_security_guards(self) -> None:
        """Structural guards: no SQL in admin_control, no financial
        primitive in the broadcast section, ONE authorization model,
        no free-form payload construction."""
        source = open(admin_control.__file__, encoding="utf-8").read()
        statement = re.compile(
            r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|PRAGMA)\b"
        )
        self.assertIsNone(statement.search(source))

        section = self._broadcast_section()
        for pattern in (
            r"\bwallet\.\w+\s*\(",
            r"\bLedgerService\b",
            r"set_rate\s*\(",
            r"update_task\s*\(",
            r"create_task\s*\(",
            r"credit_units\s*\(",
            r"reserve\s*\(",
            r"settle_units\s*\(",
            r"add_admin_user\s*\(",
            r"remove_admin_user\s*\(",
        ):
            self.assertIsNone(
                re.search(pattern, section), f"forbidden call: {pattern}"
            )
        # Single authorization model — the imported config.is_admin.
        self.assertIs(admin_control.is_admin, config.is_admin)
        self.assertIn("is_admin(actor)", section)
        # Closed grammar: only fixed broadcast tokens are built.
        self.assertNotIn("json", section.lower())
        self.assertNotIn("f\"{CALLBACK_PREFIX}{OP_BROADCAST}:\" +", section)

    def test_51_no_secrets_in_output_or_logs(self) -> None:
        """Every broadcast surface and log line carries only safe
        ids and fixed text — never tokens, env values or paths."""
        self._seed_users(*RECIPIENTS)
        texts = []
        _u, panel, _m = self._view("ctl:broadcast")
        texts.append(panel)
        prompt = self._press("ctl:broadcast:new")
        texts.append(_edited(prompt.callback_query))
        with self.assertLogs("admin_control", level="INFO") as logs:
            composed = self._send_text(BODY)
            context = self._ctx([None, Forbidden("blocked")])
            result = self._press("ctl:broadcast:confirm", context=context)
        texts.append(_reply(composed))
        texts.append(_edited(result.callback_query))
        for text in texts:
            self._assert_no_secrets(self, text)
        joined = "\n".join(logs.output)
        for needle in _FORBIDDEN_OUTPUT:
            self.assertNotIn(needle, joined.lower())
        self.assertIsNone(_TOKEN_RE.search(joined))

    @staticmethod
    def _broadcast_section() -> str:
        source = open(admin_control.__file__, encoding="utf-8").read()
        marker = "# ── Broadcast module (MT-ADMIN-38)"
        return source[source.index(marker):]


if __name__ == "__main__":
    unittest.main()
