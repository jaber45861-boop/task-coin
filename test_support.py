"""
Focused tests — Persistent Telegram User Support (MT-ADMIN-06)
===============================================================

Support system on the Telegram control plane:

    /support → category → message → persistent SupportInquiry
    → AdminNotifier → admin private chat
    → [ فتح ] [ 💬 رد ] [ ✅ إغلاق ] → persisted reply context
    → delivery to the inquiry's user

Coverage required by MT-ADMIN-06 section 17:

A. User entry
   - /support private user → category keyboard; private admin → queue
   - category selection persisted; valid message creates inquiry
   - empty / unsafe-control / over-long text rejected
   - group/channel invocation stays silent (command AND text)
B. Persistence
   - inquiry + messages survive a fresh DB connection (restart)
   - conversation history retained in deterministic order
   - one active inquiry per user (DB-enforced, not just app checks)
   - closed inquiry permits a new inquiry
   - user pending state + admin reply context survive restart
C. Admin notification
   - new inquiry notifies every configured ADMINS chat
   - never targets non-admin chats
   - notification contains only safe operational fields
   - operation → admin message linkage persists (operation_type +
     inquiry id + admin chat/message)
   - duplicate/replayed submission creates no duplicate inquiry/message
D. Admin authorization
   - admin can list / open / reply / close
   - non-admin denied everywhere (no queue data, no context, no close)
   - forged / garbage inquiry ids and callback payloads rejected
E. Reply flow
   - reply context persists (DB), survives restart simulation
   - reply goes to the inquiry's server-side user; callback cannot
     choose the recipient
   - admin message persisted BEFORE delivery; delivery failure keeps
     the message (undelivered), returns an operational error, and a
     safe retry delivers exactly once
F. Close
   - close works, is idempotent; stale reply after close rejected
   - closed inquiry cannot receive admin replies; user may open a new
     inquiry afterwards
G. Concurrency
   - two admins opening the same inquiry never mutate history
   - close vs reply safe in both orders; duplicate callbacks never
     create duplicate replies
H. Registration + regression
   - bot.py registers /support, the text handler in its own group and
     both callback patterns; existing suites stay green (full run)

Run:
    python3 -m pytest test_support.py -v
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

import bot as bot_mod
import config
import db
import support_service
import support_store
from admin_notification_store import AdminNotificationStore
from admin_notifier import AdminNotifier
from support_service import (
    MSG_ADMIN_ONLY,
    MSG_ALREADY_CLOSED,
    MSG_CATEGORY_HEADER,
    MSG_CLOSED_CARD,
    MSG_DELIVERY_FAILED,
    MSG_INVALID,
    MSG_INVALID_TEXT,
    MSG_INQUIRY_CLOSED,
    MSG_INQUIRY_GONE,
    MSG_NO_INQUIRIES,
    MSG_NOT_USER_FLOW,
    MSG_PROMPT,
    MSG_QUEUE_HEADER,
    MSG_REPLY_DUPLICATE,
    MSG_REPLY_PROMPT,
    MSG_REPLY_SENT,
    MSG_USER_APPENDED,
    MSG_USER_CREATED,
    OPERATION_SUPPORT,
)
from support_store import (
    CATEGORIES,
    CATEGORY_LABELS,
    MAX_MESSAGE_LENGTH,
    STATUS_CLOSED,
    STATUS_OPEN,
)

# ── Identities ────────────────────────────────────────────────────────
ADMIN_A = 111111
ADMIN_B = 222222
STRANGER = 999999
USER_1 = 5001
USER_2 = 5002

_SAFE_EXCERPT = "أحتاج مساعدة في المهمة رقم 5"


def _run(coroutine):
    """Run one handler coroutine synchronously (tests are sync)."""
    return asyncio.run(coroutine)


# ── Update / context builders ─────────────────────────────────────────


def _user_update(
    user_id: int,
    text: str | None = None,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
    message_id: int = 1,
    username: str | None = None,
    first_name: str | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_user.first_name = first_name
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_chat.id = (
        chat_id if chat_id is not None else user_id
    )
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = message_id
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _callback_update(
    user_id: int,
    data: str | None,
    *,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = chat_type
    update.effective_chat.id = (
        chat_id if chat_id is not None else user_id
    )
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _context(send_effect=None) -> MagicMock:
    ctx = MagicMock()
    ctx.bot = MagicMock()
    if send_effect is None:
        ctx.bot.send_message = AsyncMock(
            return_value=MagicMock(message_id=4242)
        )
    else:
        ctx.bot.send_message = AsyncMock(side_effect=send_effect)
    return ctx


def _reply_text(update) -> str:
    """First positional arg of the handler's reply_text call."""
    return update.message.reply_text.call_args[0][0]


def _answered(query) -> str | None:
    """Text passed to query.answer(), or None when answered silently."""
    args, kwargs = query.answer.call_args
    if kwargs.get("text") is not None:
        return kwargs["text"]
    if args:
        return args[0]
    return None


def _edited_text(query) -> str:
    return query.edit_message_text.call_args[0][0]


# ── Shared fixture ────────────────────────────────────────────────────


class SupportTestBase(unittest.TestCase):
    """Temp DB + patched ADMINS + captured AdminNotifier transports."""

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.addCleanup(self._restore_db)

        # Patch config.ADMINS in place (same list object the notifier
        # imported at module load).
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_A, ADMIN_B]
        self.addCleanup(self._restore_admins)

        # Captured transports behind the REAL AdminNotifier.
        self.sent: list[dict] = []
        self._msg_seq = 0

        async def _send_text(chat_id: int, text: str) -> None:
            self.sent.append(
                {"kind": "text", "chat_id": chat_id, "text": text}
            )

        async def _send_markup(chat_id: int, text: str, reply_markup) -> int:
            self._msg_seq += 1
            self.sent.append(
                {
                    "kind": "markup",
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "message_id": self._msg_seq,
                }
            )
            return self._msg_seq

        support_service.bind(
            AdminNotifier(_send_text, markup_send=_send_markup)
        )
        self.addCleanup(support_service.unbind)

    # ── cleanup helpers ───────────────────────────────────────────────

    def _restore_db(self) -> None:
        db.DB_PATH = self._orig_db_path

    def _restore_admins(self) -> None:
        config.ADMINS[:] = self._orig_admins

    # ── raw (fresh-connection) readers: restart simulation ────────────

    def _raw(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _inquiry_rows(self) -> list:
        return self._raw(
            "SELECT id, user_id, category, status FROM support_inquiries "
            "ORDER BY id"
        )

    def _message_rows(self) -> list:
        return self._raw(
            "SELECT id, inquiry_id, sender_type, sender_id, message, "
            "source_message_id, delivered_at FROM support_messages "
            "ORDER BY id"
        )

    def _context_rows(self) -> list:
        return self._raw(
            "SELECT admin_user_id, admin_chat_id, inquiry_id "
            "FROM support_reply_contexts ORDER BY admin_user_id"
        )

    # ── flow helpers ──────────────────────────────────────────────────

    def _user_submits(
        self,
        user_id: int,
        text: str,
        *,
        category: str = "task",
        message_id: int = 1,
        chat_type: str = "private",
        ctx: MagicMock | None = None,
    ) -> MagicMock:
        """Drive the user text handler with a persisted pending state."""
        support_store.set_pending_category(user_id, category)
        update = _user_update(
            user_id, text, chat_type=chat_type, message_id=message_id
        )
        _run(
            support_service.support_text_input(update, ctx or _context())
        )
        return update

    def _admin_press(
        self, admin_id: int, data: str, *, chat_type: str = "private"
    ) -> MagicMock:
        update = _callback_update(admin_id, data, chat_type=chat_type)
        _run(support_service.support_admin_callback(update, _context()))
        return update

    def _admin_reply(
        self,
        admin_id: int,
        text: str,
        *,
        message_id: int = 77,
        ctx: MagicMock | None = None,
    ) -> MagicMock:
        update = _user_update(admin_id, text, message_id=message_id)
        _run(
            support_service.support_text_input(update, ctx or _context())
        )
        return update


# ══════════════════════════════════════════════════════════════════════
# A. USER ENTRY
# ══════════════════════════════════════════════════════════════════════


class TestUserEntry(SupportTestBase):
    def test_support_private_user_gets_category_keyboard(self) -> None:
        update = _user_update(USER_1, "/support")
        _run(support_service.support_command(update, _context()))

        text = _reply_text(update)
        self.assertIn(MSG_CATEGORY_HEADER, text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        labels = [
            btn.text
            for row in markup.inline_keyboard
            for btn in row
        ]
        self.assertEqual(
            labels,
            [CATEGORY_LABELS[c] for c in CATEGORIES],
        )
        callbacks = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
        ]
        self.assertEqual(
            callbacks, [f"supcat:{c}" for c in CATEGORIES]
        )
        # Fresh entry never resurrects an old pending selection.
        self.assertIsNone(support_store.get_pending_category(USER_1))

    def test_support_group_invocation_is_silent(self) -> None:
        update = _user_update(
            USER_1, "/support", chat_type="supergroup", chat_id=-100555
        )
        _run(support_service.support_command(update, _context()))
        update.message.reply_text.assert_not_called()
        self.assertEqual(self._inquiry_rows(), [])

    def test_admin_support_shows_queue_not_user_flow(self) -> None:
        update = _user_update(ADMIN_A, "/support")
        _run(support_service.support_command(update, _context()))
        text = _reply_text(update)
        self.assertIn(MSG_NO_INQUIRIES, text)
        self.assertNotIn(MSG_CATEGORY_HEADER, text)
        self.assertIsNone(support_store.get_pending_category(ADMIN_A))

    def test_admin_support_queue_lists_open_inquiries(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=11)
        inquiry = support_store.get_active_inquiry(USER_1)
        update = _user_update(ADMIN_A, "/support")
        _run(support_service.support_command(update, _context()))
        text = _reply_text(update)
        self.assertIn(MSG_QUEUE_HEADER, text)
        self.assertIn(f"#{inquiry.id}", text)
        self.assertIn(CATEGORY_LABELS["task"], text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        callbacks = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
        ]
        self.assertIn(f"sup:open:{inquiry.id}", callbacks)

    def test_category_selection_is_persisted_state(self) -> None:
        update = _callback_update(USER_1, "supcat:withdrawal")
        _run(support_service.support_category_callback(update, _context()))

        # Server-side state, not handler memory.
        self.assertEqual(
            support_store.get_pending_category(USER_1), "withdrawal"
        )
        rows = self._raw(
            "SELECT category FROM support_user_states WHERE user_id = ?",
            (USER_1,),
        )
        self.assertEqual(rows[0]["category"], "withdrawal")
        prompt = _edited_text(update.callback_query)
        self.assertIn(MSG_PROMPT, prompt)
        update.callback_query.edit_message_text.assert_called_once()

    def test_admin_category_press_is_denied(self) -> None:
        update = _callback_update(ADMIN_A, "supcat:task")
        _run(support_service.support_category_callback(update, _context()))
        self.assertEqual(_answered(update.callback_query), MSG_NOT_USER_FLOW)
        self.assertIsNone(support_store.get_pending_category(ADMIN_A))
        update.callback_query.edit_message_text.assert_not_called()

    def test_invalid_category_payload_rejected(self) -> None:
        for bad in ("supcat:hacks", "supcat:", "supcat:TASK", None, "x"):
            update = _callback_update(USER_1, bad)
            _run(
                support_service.support_category_callback(
                    update, _context()
                )
            )
            self.assertEqual(
                _answered(update.callback_query), MSG_INVALID, bad
            )
        self.assertIsNone(support_store.get_pending_category(USER_1))

    def test_valid_message_creates_inquiry_and_notifies(self) -> None:
        update = self._user_submits(USER_1, _SAFE_EXCERPT, message_id=21)

        inquiry = support_store.get_active_inquiry(USER_1)
        self.assertIsNotNone(inquiry)
        self.assertEqual(inquiry.user_id, USER_1)
        self.assertEqual(inquiry.category, "task")
        self.assertEqual(inquiry.status, STATUS_OPEN)

        messages = support_store.list_messages(inquiry.id, limit=10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].sender_type, "user")
        self.assertEqual(messages[0].sender_id, USER_1)
        self.assertEqual(messages[0].message, _SAFE_EXCERPT)

        reply = _reply_text(update)
        self.assertIn(f"#{inquiry.id}", reply)
        self.assertIn(str(inquiry.id), MSG_USER_CREATED.format(
            inquiry_id=inquiry.id
        ))

        # Pending selection consumed by the submission.
        self.assertIsNone(support_store.get_pending_category(USER_1))
        # Admins were notified.
        self.assertEqual(len(self.sent), 2)

    def test_existing_open_inquiry_appends_with_notice(self) -> None:
        self._user_submits(USER_1, "المشكلة الأولى", message_id=31)
        inquiry = support_store.get_active_inquiry(USER_1)
        update = self._user_submits(
            USER_1, "رسالة متابعة", category="account", message_id=32
        )

        reply = _reply_text(update)
        self.assertIn(MSG_USER_APPENDED.format(inquiry_id=inquiry.id), reply)
        # Still exactly ONE inquiry, original category untouched.
        open_rows = [
            r for r in self._inquiry_rows() if r["status"] == STATUS_OPEN
        ]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["id"], inquiry.id)
        self.assertEqual(open_rows[0]["category"], "task")
        self.assertEqual(len(self._message_rows()), 2)

    def test_empty_message_rejected(self) -> None:
        update = self._user_submits(USER_1, "    \n  ", message_id=41)
        self.assertEqual(_reply_text(update), MSG_INVALID_TEXT)
        self.assertEqual(self._inquiry_rows(), [])
        self.assertEqual(self._message_rows(), [])
        # User stays in the flow (pending state preserved).
        self.assertEqual(
            support_store.get_pending_category(USER_1), "task"
        )
        self.assertEqual(self.sent, [])

    def test_control_characters_rejected(self) -> None:
        for bad in ("hello\x00world", "hi\x07", "x\x7fy"):
            update = self._user_submits(
                USER_1, bad, message_id=42
            )
            self.assertEqual(_reply_text(update), MSG_INVALID_TEXT, bad)
        self.assertEqual(self._inquiry_rows(), [])
        self.assertEqual(self._message_rows(), [])

    def test_over_long_message_rejected(self) -> None:
        update = self._user_submits(
            USER_1, "ا" * (MAX_MESSAGE_LENGTH + 1), message_id=43
        )
        self.assertEqual(_reply_text(update), MSG_INVALID_TEXT)
        self.assertEqual(self._inquiry_rows(), [])

    def test_multiline_message_accepted(self) -> None:
        update = self._user_submits(
            USER_1, "سطر أول\nسطر ثانٍ", message_id=44
        )
        inquiry = support_store.get_active_inquiry(USER_1)
        self.assertIsNotNone(inquiry)
        messages = support_store.list_messages(inquiry.id)
        self.assertEqual(messages[0].message, "سطر أول\nسطر ثانٍ")
        self.assertIn(f"#{inquiry.id}", _reply_text(update))

    def test_group_text_with_pending_state_stays_silent(self) -> None:
        support_store.set_pending_category(USER_1, "task")
        update = _user_update(
            USER_1,
            "رسالة من مجموعة",
            chat_type="supergroup",
            chat_id=-100777,
            message_id=51,
        )
        _run(support_service.support_text_input(update, _context()))
        update.message.reply_text.assert_not_called()
        self.assertEqual(self._inquiry_rows(), [])
        self.assertEqual(self._message_rows(), [])

    def test_text_without_pending_state_is_silent(self) -> None:
        update = _user_update(USER_1, "ملاحظة عادية", message_id=52)
        _run(support_service.support_text_input(update, _context()))
        update.message.reply_text.assert_not_called()
        self.assertEqual(self._inquiry_rows(), [])
        # Ordinary chat never even reaches the bot transport.
        ctx = _context()
        _run(support_service.support_text_input(update, ctx))
        ctx.bot.send_message.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# B. PERSISTENCE
# ══════════════════════════════════════════════════════════════════════


class TestPersistence(SupportTestBase):
    def test_inquiry_and_messages_survive_fresh_connection(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=61)
        inquiry = support_store.get_active_inquiry(USER_1)

        # "Restart": every reader below opens its OWN connection.
        rows = self._inquiry_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], inquiry.id)
        self.assertEqual(rows[0]["user_id"], USER_1)
        self.assertEqual(rows[0]["status"], STATUS_OPEN)
        self.assertEqual(rows[0]["category"], "task")

        msg_rows = self._message_rows()
        self.assertEqual(len(msg_rows), 1)
        self.assertEqual(msg_rows[0]["message"], _SAFE_EXCERPT)

        # Module state is entirely DB-backed: a fresh read resolves it.
        self.assertIsNotNone(support_store.get_inquiry(inquiry.id))
        self.assertIsNotNone(support_store.get_active_inquiry(USER_1))

    def test_conversation_history_retained_in_order(self) -> None:
        self._user_submits(USER_1, "المشكلة في المهمة", message_id=62)
        inquiry = support_store.get_active_inquiry(USER_1)

        support_store.set_reply_context(ADMIN_A, ADMIN_A, inquiry.id)
        admin_msg = support_store.append_admin_message(
            inquiry.id, ADMIN_A, "ابعت الإثبات من فضلك", 8801
        )
        self.assertIsNotNone(admin_msg)

        self._user_submits(USER_1, "تفضل الإثبات", message_id=63)
        support_store.append_admin_message(
            inquiry.id, ADMIN_B, "تم حل المشكلة", 8802
        )

        history = support_store.list_messages(inquiry.id, limit=10)
        self.assertEqual(
            [(m.sender_type, m.message) for m in history],
            [
                ("user", "المشكلة في المهمة"),
                ("admin", "ابعت الإثبات من فضلك"),
                ("user", "تفضل الإثبات"),
                ("admin", "تم حل المشكلة"),
            ],
        )
        # Restart simulation: raw rows keep the same deterministic order.
        raw = self._message_rows()
        self.assertEqual(
            [(r["sender_type"], r["message"]) for r in raw],
            [(m.sender_type, m.message) for m in history],
        )

    def test_one_active_inquiry_per_user_db_enforced(self) -> None:
        self._user_submits(USER_1, "رسالة أولى", message_id=64)
        self._user_submits(
            USER_1, "رسالة ثانية", category="deposit", message_id=65
        )
        open_rows = [
            r for r in self._inquiry_rows() if r["status"] == STATUS_OPEN
        ]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["category"], "task")

        # DB-level constraint: a second OPEN row is impossible even
        # when the application check is bypassed.
        with self.assertRaises(sqlite3.IntegrityError):
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT INTO support_inquiries "
                    "(user_id, category, status) VALUES (?, ?, ?)",
                    (USER_1, "other", STATUS_OPEN),
                )
                conn.commit()
            finally:
                conn.close()

    def test_closed_inquiry_permits_new_inquiry(self) -> None:
        self._user_submits(USER_1, "طلب قديم", message_id=66)
        first = support_store.get_active_inquiry(USER_1)
        self.assertTrue(support_store.close_inquiry(first.id))

        self.assertIsNone(support_store.get_active_inquiry(USER_1))
        self._user_submits(USER_1, "طلب جديد", message_id=67)
        second = support_store.get_active_inquiry(USER_1)

        self.assertIsNotNone(second)
        self.assertNotEqual(second.id, first.id)
        rows = self._inquiry_rows()
        statuses = {r["id"]: r["status"] for r in rows}
        self.assertEqual(statuses[first.id], STATUS_CLOSED)
        self.assertEqual(statuses[second.id], STATUS_OPEN)

    def test_user_pending_state_survives_restart(self) -> None:
        support_store.set_pending_category(USER_1, "deposit")
        rows = self._raw(
            "SELECT category FROM support_user_states WHERE user_id = ?",
            (USER_1,),
        )
        self.assertEqual(rows[0]["category"], "deposit")
        # Fresh reader resolves it with no in-memory seeding.
        self.assertEqual(
            support_store.get_pending_category(USER_1), "deposit"
        )

    def test_reply_context_survives_restart(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=68)
        inquiry = support_store.get_active_inquiry(USER_1)
        support_store.set_reply_context(ADMIN_A, ADMIN_A, inquiry.id)

        rows = self._context_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["admin_user_id"], ADMIN_A)
        self.assertEqual(rows[0]["inquiry_id"], inquiry.id)

        ctx = support_store.get_reply_context(ADMIN_A, ADMIN_A)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.inquiry_id, inquiry.id)
        # Wrong chat id does not resolve the context.
        self.assertIsNone(
            support_store.get_reply_context(ADMIN_A, ADMIN_A + 1)
        )


# ══════════════════════════════════════════════════════════════════════
# C. ADMIN NOTIFICATION
# ══════════════════════════════════════════════════════════════════════


class TestAdminNotification(SupportTestBase):
    def test_new_inquiry_notifies_every_admin(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=71)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(
            sorted(s["chat_id"] for s in self.sent),
            sorted([ADMIN_A, ADMIN_B]),
        )
        inquiry = support_store.get_active_inquiry(USER_1)
        for entry in self.sent:
            self.assertIn(f"#{inquiry.id}", entry["text"])
            self.assertIn(CATEGORY_LABELS["task"], entry["text"])
            self.assertIn(str(USER_1), entry["text"])
            self.assertIn(_SAFE_EXCERPT, entry["text"])
            self.assertIn("مفتوح", entry["text"])  # status
            self.assertIsInstance(entry["reply_markup"], InlineKeyboardMarkup)

    def test_notification_never_targets_non_admin_chats(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=72)
        allowed = {ADMIN_A, ADMIN_B}
        self.assertTrue(
            all(s["chat_id"] in allowed for s in self.sent),
            f"non-admin target in {[s['chat_id'] for s in self.sent]}",
        )
        self.assertNotIn(
            STRANGER, [s["chat_id"] for s in self.sent]
        )

    def test_notification_contains_only_safe_fields(self) -> None:
        self._user_submits(
            USER_1,
            _SAFE_EXCERPT,
            message_id=73,
        )
        inquiry = support_store.get_active_inquiry(USER_1)
        text = "\n".join(s["text"] for s in self.sent).lower()
        for forbidden in (
            "task_data",
            "wallet",
            "ledger",
            "initdata",
            "token",
            "password",
            "secret",
            "balance",
        ):
            self.assertNotIn(forbidden, text)
        # Required safe fields ARE present.
        self.assertIn(str(inquiry.id), text)
        self.assertIn(str(USER_1), text)
        self.assertIn(_SAFE_EXCERPT.lower(), text)

    def test_notification_linkage_persists(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=74)
        inquiry = support_store.get_active_inquiry(USER_1)

        self.assertTrue(
            AdminNotificationStore.has_linkage(
                OPERATION_SUPPORT, inquiry.id
            )
        )
        linkages = AdminNotificationStore.list_for_operation(
            OPERATION_SUPPORT, inquiry.id
        )
        self.assertEqual(
            sorted(l.admin_chat_id for l in linkages),
            sorted([ADMIN_A, ADMIN_B]),
        )
        # operation type + inquiry id + admin chat/message linkage
        for linkage in linkages:
            self.assertEqual(linkage.operation_type, OPERATION_SUPPORT)
            self.assertEqual(linkage.operation_id, inquiry.id)
            self.assertGreater(linkage.message_id, 0)

        # Callback payload is only a lookup pointer to that inquiry.
        update = _callback_update(
            ADMIN_A, f"sup:open:{inquiry.id}"
        )
        _run(support_service.support_admin_callback(update, _context()))
        self.assertIn(
            f"#{inquiry.id}", _edited_text(update.callback_query)
        )

    def test_replayed_submission_creates_no_duplicate(self) -> None:
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=75)
        inquiry = support_store.get_active_inquiry(USER_1)
        sent_after_first = len(self.sent)

        # Same Telegram update replayed (same source message id).
        update = self._user_submits(
            USER_1, _SAFE_EXCERPT, message_id=75
        )
        self.assertIn(f"#{inquiry.id}", _reply_text(update))
        self.assertEqual(len(self._inquiry_rows()), 1)
        self.assertEqual(len(self._message_rows()), 1)
        # The replay does not re-notify admins.
        self.assertEqual(len(self.sent), sent_after_first)

    def test_unbound_notifier_fails_soft(self) -> None:
        support_service.unbind()
        try:
            self._user_submits(USER_1, _SAFE_EXCERPT, message_id=76)
        finally:
            support_service.bind(
                AdminNotifier(
                    _noop_send, markup_send=_noop_markup_send
                )
            )
        inquiry = support_store.get_active_inquiry(USER_1)
        self.assertIsNotNone(inquiry)
        self.assertEqual(len(self._message_rows()), 1)
        self.assertEqual(self.sent, [])


async def _noop_send(chat_id: int, text: str) -> None:
    return None


async def _noop_markup_send(chat_id: int, text: str, reply_markup) -> int:
    return 1


# ══════════════════════════════════════════════════════════════════════
# D. ADMIN AUTHORIZATION
# ══════════════════════════════════════════════════════════════════════


class TestAdminAuthorization(SupportTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=81)
        self.inquiry = support_store.get_active_inquiry(USER_1)

    def test_admin_can_list(self) -> None:
        update = _user_update(ADMIN_B, "/support")
        _run(support_service.support_command(update, _context()))
        text = _reply_text(update)
        self.assertIn(MSG_QUEUE_HEADER, text)
        self.assertIn(f"#{self.inquiry.id}", text)

    def test_non_admin_list_never_sees_queue_data(self) -> None:
        update = _user_update(STRANGER, "/support")
        _run(support_service.support_command(update, _context()))
        text = _reply_text(update)
        self.assertIn(MSG_CATEGORY_HEADER, text)
        self.assertNotIn(f"#{self.inquiry.id}", text)
        self.assertNotIn(MSG_QUEUE_HEADER, text)

    def test_admin_can_open(self) -> None:
        update = self._admin_press(ADMIN_A, f"sup:open:{self.inquiry.id}")
        text = _edited_text(update.callback_query)
        self.assertIn(f"#{self.inquiry.id}", text)
        self.assertIn(CATEGORY_LABELS["task"], text)
        self.assertIn(str(USER_1), text)
        self.assertIn("مفتوح", text)
        self.assertIn(_SAFE_EXCERPT, text)
        markup = update.callback_query.edit_message_text.call_args[1][
            "reply_markup"
        ]
        callbacks = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
        ]
        self.assertIn(f"sup:reply:{self.inquiry.id}", callbacks)
        self.assertIn(f"sup:close:{self.inquiry.id}", callbacks)

    def test_non_admin_open_denied(self) -> None:
        update = self._admin_press(STRANGER, f"sup:open:{self.inquiry.id}")
        query = update.callback_query
        self.assertEqual(_answered(query), MSG_ADMIN_ONLY)
        query.edit_message_text.assert_not_called()

    def test_admin_can_enter_reply_mode(self) -> None:
        update = self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        query = update.callback_query
        self.assertIn(str(self.inquiry.id), _edited_text(query))
        ctx = support_store.get_reply_context(ADMIN_A, ADMIN_A)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.inquiry_id, self.inquiry.id)

    def test_non_admin_reply_denied(self) -> None:
        update = self._admin_press(STRANGER, f"sup:reply:{self.inquiry.id}")
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        self.assertIsNone(
            support_store.get_reply_context(STRANGER, STRANGER)
        )
        self.assertEqual(self._context_rows(), [])

    def test_admin_can_close(self) -> None:
        update = self._admin_press(ADMIN_B, f"sup:close:{self.inquiry.id}")
        self.assertIn(
            MSG_CLOSED_CARD.format(inquiry_id=self.inquiry.id),
            _edited_text(update.callback_query),
        )
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_CLOSED)

    def test_non_admin_close_denied(self) -> None:
        update = self._admin_press(STRANGER, f"sup:close:{self.inquiry.id}")
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_OPEN)

    def test_forged_inquiry_id_rejected(self) -> None:
        for op in ("open", "reply", "close", "cancel"):
            update = self._admin_press(ADMIN_A, f"sup:{op}:424242")
            self.assertEqual(
                _answered(update.callback_query),
                MSG_INQUIRY_GONE,
                op,
            )
        # Nothing changed for the real inquiry.
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_OPEN)
        self.assertEqual(self._context_rows(), [])

    def test_garbage_callback_payloads_rejected(self) -> None:
        for bad in (
            "sup:open:abc",
            "sup:open:-5",
            "sup:open:0",
            "sup:hack:1",
            "supx:open:1",
            "sup:open",
            "sup:open:1:2",
            "",
            None,
        ):
            update = self._admin_press(ADMIN_A, bad)
            self.assertEqual(
                _answered(update.callback_query), MSG_INVALID, bad
            )
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_OPEN)

    def test_non_admin_text_never_resolves_a_context(self) -> None:
        # Even a crafted text from a non-admin in private chat must
        # never reach a user or touch a message row.
        ctx = _context()
        update = _user_update(STRANGER, "hello", message_id=91)
        _run(support_service.support_text_input(update, ctx))
        ctx.bot.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        self.assertEqual(len(self._message_rows()), 1)  # only user msg


# ══════════════════════════════════════════════════════════════════════
# E. REPLY FLOW
# ══════════════════════════════════════════════════════════════════════


class TestReplyFlow(SupportTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=101)
        self.inquiry = support_store.get_active_inquiry(USER_1)

    def _enter_reply_mode(self, admin_id: int = ADMIN_A) -> None:
        self._admin_press(admin_id, f"sup:reply:{self.inquiry.id}")

    def test_reply_context_persists_across_restart(self) -> None:
        self._enter_reply_mode()
        rows = self._context_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["admin_user_id"], ADMIN_A)
        self.assertEqual(rows[0]["inquiry_id"], self.inquiry.id)
        # Restart simulation: resolve purely from a fresh DB read.
        ctx = support_store.get_reply_context(ADMIN_A, ADMIN_A)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.inquiry_id, self.inquiry.id)

    def test_reply_delivered_to_server_side_user(self) -> None:
        self._enter_reply_mode()
        ctx = _context()
        update = self._admin_reply(
            ADMIN_A, "ابعت الإثبات من فضلك", message_id=901, ctx=ctx
        )

        kwargs = ctx.bot.send_message.call_args[1]
        self.assertEqual(kwargs["chat_id"], USER_1)  # from inquiry row
        self.assertIn(f"#{self.inquiry.id}", kwargs["text"])
        self.assertIn("ابعت الإثبات من فضلك", kwargs["text"])

        self.assertEqual(_reply_text(update), MSG_REPLY_SENT)
        messages = support_store.list_messages(self.inquiry.id, limit=10)
        self.assertEqual(len(messages), 2)
        admin_msg = messages[1]
        self.assertEqual(admin_msg.sender_type, "admin")
        self.assertEqual(admin_msg.sender_id, ADMIN_A)
        self.assertTrue(admin_msg.delivered)
        self.assertIsNotNone(admin_msg.delivered_at)

    def test_callback_cannot_choose_recipient(self) -> None:
        # Second inquiry owned by USER_2.
        self._user_submits(USER_2, "مشكلة لدي", message_id=102)
        inquiry2 = support_store.get_active_inquiry(USER_2)

        # Admin replies into inquiry 2: recipient must be USER_2 —
        # derived from the persisted inquiry, NOT the callback.
        self._admin_press(ADMIN_B, f"sup:reply:{inquiry2.id}")
        ctx = _context()
        self._admin_reply(
            ADMIN_B, "تم الاستلام", message_id=902, ctx=ctx
        )
        self.assertEqual(
            ctx.bot.send_message.call_args[1]["chat_id"], USER_2
        )

        # Callback payloads carrying an extra recipient field are
        # rejected outright — they can never steer delivery.
        for bad in (f"sup:reply:{inquiry2.id}:{USER_1}", "sup:reply:1;2"):
            update = self._admin_press(ADMIN_A, bad)
            self.assertEqual(
                _answered(update.callback_query), MSG_INVALID, bad
            )

    def test_delivery_failure_preserves_message(self) -> None:
        self._enter_reply_mode()
        ctx = _context(send_effect=TelegramError("boom"))
        update = self._admin_reply(
            ADMIN_A, "رسالة تجريبية", message_id=903, ctx=ctx
        )

        self.assertEqual(_reply_text(update), MSG_DELIVERY_FAILED)
        rows = self._message_rows()
        self.assertEqual(len(rows), 2)
        admin_row = rows[1]
        self.assertEqual(admin_row["sender_type"], "admin")
        self.assertIsNone(
            admin_row["delivered_at"]
        )  # never faked as delivered

    def test_safe_retry_after_delivery_failure(self) -> None:
        self._enter_reply_mode()
        failing = _context(send_effect=TelegramError("boom"))
        self._admin_reply(
            ADMIN_A, "رسالة تجريبية", message_id=904, ctx=failing
        )
        self.assertEqual(len(self._message_rows()), 2)

        # Replay of the SAME Telegram update with a working transport
        # retries delivery without duplicating the stored message.
        working = _context()
        update = self._admin_reply(
            ADMIN_A, "رسالة تجريبية", message_id=904, ctx=working
        )
        self.assertEqual(_reply_text(update), MSG_REPLY_SENT)
        self.assertEqual(len(self._message_rows()), 2)
        working.bot.send_message.assert_called_once()
        rows = self._message_rows()
        self.assertIsNotNone(rows[1]["delivered_at"])

        # A further replay after success never re-delivers.
        again = _context()
        update = self._admin_reply(
            ADMIN_A, "رسالة تجريبية", message_id=904, ctx=again
        )
        self.assertEqual(_reply_text(update), MSG_REPLY_DUPLICATE)
        again.bot.send_message.assert_not_called()

    def test_admin_text_without_context_is_silent(self) -> None:
        ctx = _context()
        update = _user_update(ADMIN_A, "مرحبا", message_id=905)
        _run(support_service.support_text_input(update, ctx))
        ctx.bot.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        self.assertEqual(len(self._message_rows()), 1)

    def test_invalid_reply_text_rejected_before_persist(self) -> None:
        self._enter_reply_mode()
        ctx = _context()
        update = self._admin_reply(
            ADMIN_A, "   ", message_id=906, ctx=ctx
        )
        self.assertEqual(_reply_text(update), MSG_INVALID_TEXT)
        ctx.bot.send_message.assert_not_called()
        self.assertEqual(len(self._message_rows()), 1)


# ══════════════════════════════════════════════════════════════════════
# F. CLOSE
# ══════════════════════════════════════════════════════════════════════


class TestClose(SupportTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=111)
        self.inquiry = support_store.get_active_inquiry(USER_1)

    def test_close_marks_inquiry_closed(self) -> None:
        update = self._admin_press(ADMIN_A, f"sup:close:{self.inquiry.id}")
        self.assertIn(
            str(self.inquiry.id), _edited_text(update.callback_query)
        )
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_CLOSED)
        self.assertIsNone(support_store.get_active_inquiry(USER_1))
        # Queue no longer lists it.
        self.assertEqual(support_store.list_open_inquiries(), [])

    def test_close_is_idempotent(self) -> None:
        self.assertTrue(support_store.close_inquiry(self.inquiry.id))
        self.assertFalse(support_store.close_inquiry(self.inquiry.id))

        update = self._admin_press(ADMIN_A, f"sup:close:{self.inquiry.id}")
        query = update.callback_query
        self.assertEqual(
            _answered(query),
            MSG_ALREADY_CLOSED.format(inquiry_id=self.inquiry.id),
        )
        self.assertEqual(
            _edited_text(query),
            MSG_CLOSED_CARD.format(inquiry_id=self.inquiry.id),
        )
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_CLOSED)
        self.assertEqual(len(self._inquiry_rows()), 1)

    def test_close_clears_reply_context(self) -> None:
        self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        self.assertEqual(len(self._context_rows()), 1)

        self._admin_press(ADMIN_B, f"sup:close:{self.inquiry.id}")
        self.assertEqual(self._context_rows(), [])

        # The admin's next text resolves nothing → fully silent.
        ctx = _context()
        update = _user_update(ADMIN_A, "رد متأخر", message_id=121)
        _run(support_service.support_text_input(update, ctx))
        ctx.bot.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        self.assertEqual(len(self._message_rows()), 1)

    def test_stale_reply_button_on_closed_inquiry_rejected(self) -> None:
        support_store.close_inquiry(self.inquiry.id)
        update = self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        self.assertEqual(_answered(update.callback_query), MSG_INQUIRY_CLOSED)
        self.assertEqual(self._context_rows(), [])

    def test_closed_inquiry_rejects_admin_append(self) -> None:
        support_store.close_inquiry(self.inquiry.id)
        result = support_store.append_admin_message(
            self.inquiry.id, ADMIN_A, "لا يجب أن يصل", 7777
        )
        self.assertIsNone(result)
        self.assertEqual(len(self._message_rows()), 1)

    def test_user_message_after_close_opens_new_inquiry(self) -> None:
        support_store.close_inquiry(self.inquiry.id)
        update = self._user_submits(USER_1, "مشكلة جديدة", message_id=122)
        new_inquiry = support_store.get_active_inquiry(USER_1)
        self.assertIsNotNone(new_inquiry)
        self.assertNotEqual(new_inquiry.id, self.inquiry.id)
        self.assertIn(
            MSG_USER_CREATED.format(inquiry_id=new_inquiry.id),
            _reply_text(update),
        )
        # Old closed inquiry retained its history.
        old_history = support_store.list_messages(self.inquiry.id, 10)
        self.assertEqual(len(old_history), 1)

    def test_open_button_on_closed_inquiry_shows_read_only(self) -> None:
        support_store.close_inquiry(self.inquiry.id)
        update = self._admin_press(ADMIN_A, f"sup:open:{self.inquiry.id}")
        query = update.callback_query
        text = _edited_text(query)
        self.assertIn("مغلق", text)
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        self.assertIsNone(markup)  # no reply/close buttons


# ══════════════════════════════════════════════════════════════════════
# G. CONCURRENCY
# ══════════════════════════════════════════════════════════════════════


class TestConcurrency(SupportTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._user_submits(USER_1, _SAFE_EXCERPT, message_id=131)
        self.inquiry = support_store.get_active_inquiry(USER_1)

    def test_two_admins_open_same_inquiry_never_mutates(self) -> None:
        before = self._message_rows()
        first = self._admin_press(ADMIN_A, f"sup:open:{self.inquiry.id}")
        second = self._admin_press(ADMIN_B, f"sup:open:{self.inquiry.id}")
        self.assertIn(f"#{self.inquiry.id}", _edited_text(first.callback_query))
        self.assertIn(f"#{self.inquiry.id}", _edited_text(second.callback_query))
        self.assertEqual(self._message_rows(), before)
        self.assertEqual(len(self._context_rows()), 0)

    def test_reply_then_close_is_safe(self) -> None:
        self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        update = self._admin_reply(ADMIN_A, "رد سريع", message_id=141)
        self.assertEqual(_reply_text(update), MSG_REPLY_SENT)

        self._admin_press(ADMIN_B, f"sup:close:{self.inquiry.id}")
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_CLOSED)
        rows = self._message_rows()
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[1]["delivered_at"])

    def test_close_then_reply_is_safe(self) -> None:
        # Reply context established first…
        self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        # …then the inquiry closes (context cleared with it).
        self._admin_press(ADMIN_B, f"sup:close:{self.inquiry.id}")
        self.assertEqual(self._context_rows(), [])

        ctx = _context()
        update = self._admin_reply(
            ADMIN_A, "رد بعد الإغلاق", message_id=142, ctx=ctx
        )
        ctx.bot.send_message.assert_not_called()
        update.message.reply_text.assert_not_called()
        self.assertEqual(len(self._message_rows()), 1)
        fresh = support_store.get_inquiry(self.inquiry.id)
        self.assertEqual(fresh.status, STATUS_CLOSED)

    def test_append_race_with_close_returns_none(self) -> None:
        support_store.close_inquiry(self.inquiry.id)
        result = support_store.append_admin_message(
            self.inquiry.id, ADMIN_A, "race", 8888
        )
        self.assertIsNone(result)

    def test_duplicate_close_callback_no_extra_effect(self) -> None:
        first = self._admin_press(ADMIN_A, f"sup:close:{self.inquiry.id}")
        second = self._admin_press(ADMIN_A, f"sup:close:{self.inquiry.id}")
        self.assertIn(
            MSG_CLOSED_CARD.format(inquiry_id=self.inquiry.id),
            _edited_text(first.callback_query),
        )
        self.assertEqual(
            _answered(second.callback_query),
            MSG_ALREADY_CLOSED.format(inquiry_id=self.inquiry.id),
        )
        self.assertEqual(len(self._inquiry_rows()), 1)

    def test_duplicate_reply_callback_creates_single_context(self) -> None:
        self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        self._admin_press(ADMIN_B, f"sup:reply:{self.inquiry.id}")
        rows = self._context_rows()
        # One row per admin — upsert, never duplicated keys.
        self.assertEqual(
            [r["admin_user_id"] for r in rows],
            sorted([ADMIN_A, ADMIN_B]),
        )

    def test_duplicate_reply_text_creates_single_message(self) -> None:
        self._admin_press(ADMIN_A, f"sup:reply:{self.inquiry.id}")
        ctx = _context()
        self._admin_reply(ADMIN_A, "رد مكرر", message_id=151, ctx=ctx)
        replay = _context()
        update = self._admin_reply(
            ADMIN_A, "رد مكرر", message_id=151, ctx=replay
        )
        self.assertEqual(_reply_text(update), MSG_REPLY_DUPLICATE)
        self.assertEqual(len(self._message_rows()), 2)
        replay.bot.send_message.assert_not_called()
        ctx.bot.send_message.assert_called_once()

    def test_paging_is_bounded_and_deterministic(self) -> None:
        for i in range(7):
            self._user_submits(
                USER_1 + 1000 + i, f"طلب رقم {i}", message_id=200 + i
            )
        inquiries = support_store.list_open_inquiries()
        self.assertEqual(
            [inq.id for inq in inquiries],
            sorted(inq.id for inq in inquiries),
        )
        text, markup = support_service.build_queue_page(inquiries, 1)
        page_buttons = [
            btn.callback_data
            for row in markup.inline_keyboard
            for btn in row
            if btn.callback_data.startswith("sup:open:")
        ]
        self.assertEqual(len(page_buttons), support_service.PAGE_SIZE)
        self.assertIn("التالي", str(markup))
        # Untrusted page ids are clamped, never out-of-range.
        text_big, markup_big = support_service.build_queue_page(
            inquiries, 999
        )
        self.assertIn(f"#{inquiries[-1].id}", text_big)


# ══════════════════════════════════════════════════════════════════════
# H. REGISTRATION (bot.py integration)
# ══════════════════════════════════════════════════════════════════════


class TestBotRegistrations(unittest.TestCase):
    """bot.py must register the MT-ADMIN-06 handlers as designed."""

    def setUp(self) -> None:
        self._saved_channels = dict(config.CHANNELS)
        config.CHANNELS.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config.CHANNELS.clear()
        config.CHANNELS.update(self._saved_channels)

    def _capture_main_handlers(self) -> list:
        from telegram.ext import ApplicationBuilder  # noqa: F401

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
            config.CHANNELS.clear()
            bot_mod.main()
        return captured

    def test_support_command_registered(self) -> None:
        from telegram.ext import CommandHandler

        captured = self._capture_main_handlers()
        matches = [
            (h, g)
            for h, g in captured
            if isinstance(h, CommandHandler) and "support" in h.commands
        ]
        self.assertEqual(len(matches), 1, "exactly one /support handler")
        handler, group = matches[0]
        self.assertIs(handler.callback, support_service.support_command)

    def test_support_text_handler_lives_in_its_own_group(self) -> None:
        from telegram.ext import MessageHandler

        captured = self._capture_main_handlers()
        support_groups = [
            g
            for h, g in captured
            if isinstance(h, MessageHandler)
            and h.callback is support_service.support_text_input
        ]
        wizard_groups = [
            g
            for h, g in captured
            if isinstance(h, MessageHandler)
            and h.callback is bot_mod.admin_task_wizard.wizard_text_input
        ]
        self.assertEqual(len(support_groups), 1)
        self.assertEqual(len(wizard_groups), 1)
        self.assertNotEqual(
            support_groups[0],
            wizard_groups[0],
            "support text must not share a group with the wizard "
            "catch-all (first-match-wins would shadow one of them)",
        )

    def test_support_text_handler_is_private_only(self) -> None:
        from telegram.ext import MessageHandler
        from test_admin_isolation import _real_update

        captured = self._capture_main_handlers()
        handler = next(
            h
            for h, _g in captured
            if isinstance(h, MessageHandler)
            and h.callback is support_service.support_text_input
        )
        self.assertFalse(
            handler.check_update(_real_update("supergroup", "hello"))
        )
        self.assertTrue(
            handler.check_update(_real_update("private", "hello"))
        )

    def test_support_callback_patterns_registered(self) -> None:
        from telegram.ext import CallbackQueryHandler

        captured = self._capture_main_handlers()
        category = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and h.callback is support_service.support_category_callback
        ]
        admin = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and h.callback is support_service.support_admin_callback
        ]
        self.assertEqual(len(category), 1)
        self.assertEqual(len(admin), 1)
        self.assertEqual(category[0][1], 5)
        self.assertEqual(admin[0][1], 5)
        # Patterns are disjoint from every existing callback family.
        self.assertEqual(
            category[0][0].pattern.pattern, r"^supcat:"
        )
        self.assertEqual(admin[0][0].pattern.pattern, r"^sup:")


if __name__ == "__main__":
    unittest.main()
