"""
Focused tests — Admin task management (MT-ADMIN-36)
====================================================

The ``tasks`` module of the Admin Control Center: an in-place task
management surface (list → detail → confirmation → mutation) built
strictly on the authoritative task store (``db.list_tasks`` /
``db.get_task``) and the EXISTING mutation contract
(``db.update_task`` — the same call ``/offtask`` makes).  Creation
delegates to the canonical ``/addtask`` wizard → ``task_creation``
service; no reward/type/repeat/delete editing exists (no safe
contract — reported as blockers).

Coverage required by MT-ADMIN-36 §TESTS (brief number → test name):

 A. AUTHORIZATION (1-3, 11)
   1   → test_01   admin can open the tasks module
   2   → test_02   non-admin cannot open it (refused BEFORE any read)
   3   → test_03   group/channel is silent
   11  → test_02   no unauthorized task read on any ctl:tasks* op
        → test_04   authorization runs BEFORE grammar (choke point)

 B. LIST (4-6)
   4   → test_05   task list uses the authoritative source
   5   → test_06   pagination is bounded (fixed PAGE_SIZE)
   6   → test_07   page is clamped; junk ids rejected pre-read
        → test_08   deterministic ordering

 C. DETAIL (7-8)
   7   → test_09   task detail uses authoritative persisted data
   8   → test_10   nonexistent task fails safely
        → test_11   detail exposes only safe administrative fields

 D. GRAMMAR / STALE (9-10)
   9   → test_12/13/14  malformed, unsupported and oversized payloads
   10  → test_15/16     stale callbacks + failing reads degrade safely

 E. MUTATIONS (12-19)
  12  → test_17   no mutation during list/detail/menu/prompt render
  13  → test_18   existing mutation service (db.update_task) used
  14  → test_18   no direct SQL / no direct create/delete in module
  15  → test_20   enable behavior (confirmation → apply)
  16  → test_21   disable behavior (+ user-task history preserved)
  17  → test_19   confirmation is required (press alone never writes)
  18  → test_22   double confirmation is safe (single-use)
  19  → test_23   concurrent/state race → fresh re-read → stale notice

 F. EDIT FLOW (allowed fields, validation, confirmation, stale,
    delegation — implemented only for the fields the existing
    ``db.update_task`` contract supports: title + description)
        → test_24..32

 G. CREATE TASK (delegation only)
        → test_33..34  ctl:tasks:new → the EXISTING /addtask entry →
           admin_task_wizard; never a second creation path

 H. SAFETY (20-22)
  20  → test_35   existing task lifecycle/reward semantics intact
  21  → test_35b  only safe update fields are ever written
  22  → test_36   financial tables unchanged by read paths (+ spies)

 I. REGRESSION (23-25)
  23  → test_37..39  no duplicate bot registration; the edit-text
      catch-all attaches LAZILY and idempotently (bot.py untouched)
  24  → test_40..42  Control Center navigation remains intact
  25  → full suite (run separately; only the documented baseline
      failure test_task_lifecycle::TestStart::
      test_already_completed_rejected is known)

Temp databases only; no production destinations or balances used.

Run:
    .venv/bin/python -m pytest test_admin_tasks.py -v
"""

from __future__ import annotations

import re
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import admin_task_wizard
import db
import task_taxonomy

from admin_control import (
    BACK_HINT,
    HEADER,
    MSG_ADMIN_ONLY,
    MSG_ERROR,
    MSG_INVALID,
    MSG_NO_PENDING,
    MSG_STALE_TASK,
    MSG_TASK_NOT_FOUND,
    TASK_DETAIL_HEADER,
    TASKS_HEADER,
    TASKS_PANEL_HEADER,
    TOAST_CANCELLED,
    TOAST_DISABLED,
    TOAST_EDITED,
    TOAST_ENABLED,
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

from test_admin_control import (
    FINANCIAL_TABLES,
    MUTATION_SPY_TARGETS,
    ControlTestBase,
)

from test_admin_users import _capture_handlers
from test_withdrawal_service import ADMIN_ID

STRANGER = 999_999

# SQL statement scan — the admin module must contain none (existing
# guard, mirrored from test_admin_control).
_SQL_STATEMENT_RE = re.compile(
    r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|PRAGMA)\b"
)


class _FakeApplication:
    """Minimal Application stand-in: real dict bot_data + recorded
    add_handler calls (proves the lazy attach contract)."""

    def __init__(self):
        self.bot_data: dict = {}
        self.added: list = []

    def add_handler(self, handler, group=0):
        self.added.append((handler, group))


class TasksTestBase(ControlTestBase):
    """MT-ADMIN-36 fixture: control-center drivers + task seeding +
    users-style view helpers + pending-state hygiene."""

    def setUp(self):
        super().setUp()
        admin_control._PENDING_TASK_EDITS.clear()
        self.addCleanup(admin_control._PENDING_TASK_EDITS.clear)

    # ── seeds (existing creation contract only) ─────────────────

    def _seed_task(
        self,
        title: str = "مهمة اختبار",
        description: str = "وصف المهمة",
        task_type: str = "telegram",
        reward: int = 1,
        active: bool = True,
    ) -> int:
        return db.create_task(
            title, description, task_type, reward,
            active=active, db_path=self.db_path,
        )

    # ── drivers ─────────────────────────────────────────────────

    def _view(self, data, **kwargs):
        """Press *data* and return (update, edited text, markup)."""
        update = self._press(data, **kwargs)
        edit = update.callback_query.edit_message_text
        text = edit.call_args[0][0]
        markup = edit.call_args[1].get("reply_markup")
        return update, text, markup

    def _labels(self, markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]

    def _send_text(self, text, actor_id: int = ADMIN_ID,
                   chat_type: str = "private"):
        update = _update(actor_id, text, chat_type=chat_type)
        _run(admin_control.task_edit_text_input(update, mock.MagicMock()))
        return update

    @contextmanager
    def _db_spies(self):
        """Patch the three store ops the module may use; yields the
        live mocks as ``(list, get, update)``."""
        with mock.patch.object(
            db, "list_tasks", wraps=db.list_tasks
        ) as list_spy, mock.patch.object(
            db, "get_task", wraps=db.get_task
        ) as get_spy, mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd_spy:
            yield list_spy, get_spy, upd_spy

    def _button_payloads(self, markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]


# ══════════════════════════════════════════════════════════════════
# A. AUTHORIZATION (1-3, 11)
# ══════════════════════════════════════════════════════════════════


class TestTasksAuthorization(TasksTestBase):

    def test_01_admin_can_open_tasks_module(self) -> None:
        """1. Private admin press renders the Task Management Panel."""
        self._seed_task("مهمة أولى")
        _u, text, markup = self._view("ctl:tasks")
        self.assertIn(TASKS_PANEL_HEADER, text)
        self.assertIn("📊 إجمالي المهام: 1", text)
        self.assertIn("🟢 النشطات: 1", text)
        self.assertIn("صفحة 1/1", text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)

    def test_02_non_admin_refused_before_any_task_read(self) -> None:
        """2 + 11. Every ``ctl:tasks*`` entry re-checks config.is_admin
        BEFORE any task read — no render, no lookup, no mutation."""
        task_id = self._seed_task()
        cases = (
            "ctl:tasks",
            "ctl:tasks:p:1",
            f"ctl:tasks:v:{task_id}",
            f"ctl:tasks:enable:{task_id}",
            f"ctl:tasks:disable:{task_id}",
            f"ctl:tasks:edit:{task_id}",
            f"ctl:tasks:field:title:{task_id}",
            f"ctl:tasks:confirm:enable:{task_id}",
            f"ctl:tasks:confirm:edit:{task_id}",
            "ctl:tasks:back",
            "ctl:tasks:new",
        )
        for data in cases:
            with self._db_spies() as (list_spy, get_spy, upd_spy):
                update = self._press(data, actor_id=STRANGER)
            list_spy.assert_not_called()
            get_spy.assert_not_called()
            upd_spy.assert_not_called()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
            update.callback_query.edit_message_text.assert_not_called()

    def test_03_group_and_channel_silent(self) -> None:
        """3. Group/channel invocations answer nothing and read
        nothing — MT-ADMIN-02 isolation."""
        task_id = self._seed_task()
        for chat_type in ("supergroup", "channel"):
            for data in ("ctl:tasks", f"ctl:tasks:v:{task_id}"):
                with self._db_spies() as (list_spy, get_spy, _upd):
                    update = self._press(data, chat_type=chat_type)
                list_spy.assert_not_called()
                get_spy.assert_not_called()
                self.assertIsNone(
                    _answered(update.callback_query), f"{chat_type} {data}"
                )
                update.callback_query.edit_message_text.assert_not_called()

    def test_04_authorization_runs_before_grammar(self) -> None:
        """AUTH + READ ORDER: a non-admin even on a MALFORMED payload
        gets the standard refusal — parsing never runs for them;
        an admin on the same payload gets the invalid answer."""
        for data in ("ctl:tasks:p:abc", "ctl:tasks:garbage", "ctl:zzz"):
            update = self._press(data, actor_id=STRANGER)
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
        admin_update = self._press("ctl:tasks:p:abc")
        self.assertEqual(
            _answered(admin_update.callback_query), MSG_INVALID
        )


# ══════════════════════════════════════════════════════════════════
# B. LIST (4-6) + ordering
# ══════════════════════════════════════════════════════════════════


class TestTasksList(TasksTestBase):

    def test_05_list_uses_authoritative_source(self) -> None:
        """4. The panel reads db.list_tasks — the ONE existing list
        operation — with no cache and no second query path."""
        self._seed_task("مهمة قائمة", active=True)
        with mock.patch.object(
            db, "list_tasks", wraps=db.list_tasks
        ) as src:
            _u, text, markup = self._view("ctl:tasks")
        src.assert_called_once()
        self.assertIn("مهمة قائمة", self._labels(markup)[0])

    def test_06_pagination_is_bounded(self) -> None:
        """5. A small fixed page size — never unbounded rows."""
        self.assertEqual(admin_control.TASKS_PAGE_SIZE, 5)
        self.assertLessEqual(admin_control.TASKS_PAGE_SIZE, 10)
        for i in range(7):
            self._seed_task(f"مهمة {i + 1}")
        _u, text, markup = self._view("ctl:tasks")
        rows = [l for l in self._labels(markup) if l.startswith("📋")]
        self.assertEqual(len(rows), admin_control.TASKS_PAGE_SIZE)
        self.assertIn("صفحة 1/2", text)
        payloads = self._button_payloads(markup)
        self.assertIn("ctl:tasks:new", payloads)
        self.assertIn("ctl:refresh", payloads)
        self.assertNotIn("ctl:tasks:p:0", payloads)  # no prev on page 1
        self.assertIn("ctl:tasks:p:1", payloads)

        _u2, text2, markup2 = self._view("ctl:tasks:p:1")
        rows2 = [l for l in self._labels(markup2) if l.startswith("📋")]
        self.assertEqual(len(rows2), 7 - admin_control.TASKS_PAGE_SIZE)
        self.assertIn("صفحة 2/2", text2)
        payloads2 = self._button_payloads(markup2)
        self.assertIn("ctl:tasks:p:0", payloads2)
        self.assertNotIn("ctl:tasks:p:2", payloads2)  # no next on last

    def test_07_page_clamped_and_junk_rejected(self) -> None:
        """6. Oversized-but-valid page → clamped to the real last
        page; junk page values never reach the store."""
        for i in range(7):
            self._seed_task(f"مهمة {i + 1}")
        _u, text, _m = self._view("ctl:tasks:p:999")
        self.assertIn("صفحة 2/2", text)  # clamped, not an error

        with self._db_spies() as (list_spy, get_spy, upd_spy):
            update = self._press("ctl:tasks:p:" + "9" * 10)  # 10 digits
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)
        list_spy.assert_not_called()
        update.callback_query.edit_message_text.assert_not_called()

        # Empty store → clamped to a stable empty first page.
        for i in range(7):
            self.assertTrue(
                db.delete_task(i + 1, db_path=self.db_path)
            )
        _u2, text2, _m2 = self._view("ctl:tasks:p:5")
        self.assertIn("📭 لا توجد مهام بعد.", text2)
        self.assertIn("صفحة 1/1", text2)

    def test_08_deterministic_ordering(self) -> None:
        """Repeated renders are byte-identical; rows follow id order."""
        for i in (3, 1, 2):
            self._seed_task(f"مهمة {i}")
        _u1, _t1, m1 = self._view("ctl:tasks")
        _u2, _t2, m2 = self._view("ctl:tasks")
        payloads1 = self._button_payloads(m1)
        payloads2 = self._button_payloads(m2)
        self.assertEqual(payloads1, payloads2)
        task_payloads = [
            p for p in payloads1
            if re.fullmatch(r"ctl:tasks:v:\d+", p)
        ]
        self.assertEqual(
            task_payloads,
            ["ctl:tasks:v:1", "ctl:tasks:v:2", "ctl:tasks:v:3"],
        )


# ══════════════════════════════════════════════════════════════════
# C. DETAIL (7-8) + active/inactive buttons
# ══════════════════════════════════════════════════════════════════


class TestTasksDetail(TasksTestBase):

    def test_09_detail_uses_authoritative_persisted_data(self) -> None:
        """7. Every card value comes from the persisted row."""
        task_id = self._seed_task(
            "عنوان المهمة", "وصف المهمة الكامل", "telegram", 2,
            active=True,
        )
        _u, text, markup = self._view(f"ctl:tasks:v:{task_id}")
        self.assertIn(TASK_DETAIL_HEADER, text)
        self.assertIn(f"🆔 المعرف: #{task_id}", text)
        self.assertIn("📌 العنوان: عنوان المهمة", text)
        self.assertIn("📝 الوصف: وصف المهمة الكامل", text)
        self.assertIn("💰 المكافأة: 2 USDT", text)
        self.assertIn("⚡ الحالة: 🟢 مفعّلة", text)
        self.assertIn("📡 النوع: telegram", text)
        self.assertIn("🔁 التكرار: مرة واحدة", text)
        self.assertRegex(text, r"📅 تاريخ الإنشاء: \d{4}-\d{2}-\d{2}")
        # Active task offers DISABLE; inactive offers ENABLE.
        payloads = self._button_payloads(markup)
        self.assertIn(f"ctl:tasks:disable:{task_id}", payloads)
        self.assertIn(f"ctl:tasks:edit:{task_id}", payloads)
        self.assertIn("ctl:tasks:back", payloads)

        off_id = self._seed_task("معطلة", active=False)
        _u2, text2, markup2 = self._view(f"ctl:tasks:v:{off_id}")
        self.assertIn("⚡ الحالة: 🔴 معطلة", text2)
        self.assertIn(
            f"ctl:tasks:enable:{off_id}", self._button_payloads(markup2)
        )

    def test_10_nonexistent_task_fails_safely(self) -> None:
        """8. Unknown ids answer the fixed notice — no crash, no
        traceback, no database detail."""
        _u, text, markup = self._view("ctl:tasks:v:424242")
        self.assertEqual(text, MSG_TASK_NOT_FOUND)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("sqlite", text.lower())
        self.assertEqual(self._button_payloads(markup), ["ctl:tasks:back"])

        _u2, text2, _m2 = self._view(
            "ctl:tasks:v:" + "9" * 15  # in-grammar, still unknown
        )
        self.assertEqual(text2, MSG_TASK_NOT_FOUND)

    def test_11_detail_exposes_only_safe_admin_fields(self) -> None:
        """7b. Every detail line is a documented administrative
        label — nothing else can leak into the card."""
        task_id = self._seed_task("عنوان", "وصف")
        _u, text, _m = self._view(f"ctl:tasks:v:{task_id}")
        allowed = ("🆔", "📌", "📝", "💰", "⚡", "📡", "🔁", "📅")
        for line in text.splitlines():
            if not line or line == TASK_DETAIL_HEADER:
                continue
            self.assertTrue(
                line.startswith(allowed), f"unexpected line: {line!r}"
            )
        # Secrets/destinations can never appear in the card.
        for forbidden in (
            "destination", "token", "secret", "password", "rpc",
            "storage_key", "api_key",
        ):
            self.assertNotIn(forbidden, text.lower())


# ══════════════════════════════════════════════════════════════════
# D. GRAMMAR + STALE CALLBACKS (9-10)
# ══════════════════════════════════════════════════════════════════


class TestTasksCallbackSafety(TasksTestBase):

    def _assert_invalid(self, data) -> None:
        with self._db_spies() as (list_spy, get_spy, upd_spy):
            update = self._press(data)
        self.assertEqual(_answered(update.callback_query), MSG_INVALID, data)
        list_spy.assert_not_called()
        get_spy.assert_not_called()
        upd_spy.assert_not_called()
        update.callback_query.edit_message_text.assert_not_called()

    def test_12_malformed_payloads_rejected(self) -> None:
        """9. Grammar rejects junk ids, junk ops, unsupported
        operations and foreign shapes — before any read."""
        for data in (
            "ctl:tasks:",
            "ctl:tasks:x",
            "ctl:tasks:p:",
            "ctl:tasks:p:abc",
            "ctl:tasks:p:-1",
            "ctl:tasks:v:",
            "ctl:tasks:v:abc",
            "ctl:tasks:v:1a",
            "ctl:tasks:enable:",
            "ctl:tasks:enable:abc",
            "ctl:tasks:field:x:1",
            "ctl:tasks:field:title:abc",
            "ctl:tasks:confirm:",
            "ctl:tasks:confirm:enable:",
            "ctl:tasks:new:1",
            "ctl:tasks:back:1",
            "ctl:tasks:501",
            "ctl:TASKS",
            "ctl:tasks ",
            None,
        ):
            self._assert_invalid(data)

    def test_13_unsupported_operations_rejected(self) -> None:
        """9b. There is NO reward, delete, type or repeat surface —
        those ops do not exist in the closed grammar."""
        for data in (
            "ctl:tasks:confirm:reward:1",
            "ctl:tasks:confirm:delete:1",
            "ctl:tasks:reward:1",
            "ctl:tasks:delete:1",
            "ctl:tasks:type:1",
            "ctl:tasks:repeat:1",
            "ctl:tasks:confirm:create:1",
            "ctl:tasks:archive:1",
        ):
            self._assert_invalid(data)
            self.assertIsNone(parse_callback(data), data)

    def test_14_oversized_ids_rejected(self) -> None:
        """9c. Beyond 15 id digits / 9 page digits → invalid."""
        self._assert_invalid("ctl:tasks:v:" + "9" * 16)
        self._assert_invalid("ctl:tasks:enable:" + "9" * 16)
        self._assert_invalid("ctl:tasks:confirm:disable:" + "9" * 16)
        self._assert_invalid("ctl:tasks:p:" + "9" * 10)

    def test_15_stale_callbacks_fail_safely(self) -> None:
        """10. Old messages/queries (edit and answer both raising)
        degrade to safe no-ops — the handler never throws."""
        self._seed_task("مهمة", active=False)
        for data in ("ctl:tasks", "ctl:tasks:v:1",
                     "ctl:tasks:enable:1", "ctl:tasks:edit:1"):
            update = _callback(ADMIN_ID, data)
            update.callback_query.message.reply_text = mock.AsyncMock()
            update.callback_query.edit_message_text = mock.AsyncMock(
                side_effect=Exception("message too old")
            )
            update.callback_query.answer = mock.AsyncMock(
                side_effect=Exception("query too old")
            )
            _run(admin_control.control_callback(update, mock.MagicMock()))
            update.callback_query.edit_message_text.assert_awaited_once()

    def test_16_render_failure_degrades_to_safe_error(self) -> None:
        """A failing authoritative read answers the fixed safe error —
        never a traceback or the internal message."""
        with mock.patch.object(
            db, "list_tasks", side_effect=RuntimeError("boom-tasks")
        ):
            update = self._press("ctl:tasks")
        answer = _answered(update.callback_query)
        self.assertEqual(answer, MSG_ERROR)
        self.assertNotIn("boom-tasks", answer)
        self.assertNotIn("Traceback", answer)
        update.callback_query.edit_message_text.assert_not_called()

        with mock.patch.object(
            db, "get_task", side_effect=RuntimeError("boom-task")
        ):
            update2 = self._press("ctl:tasks:v:1")
        answer2 = _answered(update2.callback_query)
        self.assertEqual(answer2, MSG_ERROR)
        self.assertNotIn("boom-task", answer2)


# ══════════════════════════════════════════════════════════════════
# E. MUTATIONS (12-19)
# ══════════════════════════════════════════════════════════════════


class TestTasksMutations(TasksTestBase):

    def test_17_rendering_is_read_only(self) -> None:
        """12. Rendering panel/detail/menu/prompt/back opens no
        transaction and calls no mutation — update_task included."""
        task_id = self._seed_task()
        with mock.patch.object(db, "transaction") as txn, \
                mock.patch.object(
                    db, "update_task", wraps=db.update_task
                ) as upd:
            self._press("ctl:tasks")
            self._press("ctl:tasks:p:1")
            self._press(f"ctl:tasks:v:{task_id}")
            self._press(f"ctl:tasks:edit:{task_id}")
            self._press(f"ctl:tasks:field:title:{task_id}")
            self._press("ctl:tasks:back")
            self._press("ctl:refresh")
        txn.assert_not_called()
        upd.assert_not_called()

    def test_18_mutations_go_through_existing_update_task(self) -> None:
        """13 + 14. Enable/disable delegates to db.update_task with
        the exact existing contract call — never raw SQL, never a
        second mutation primitive, never direct create/delete."""
        task_id = self._seed_task("مهمة", active=False)

        # The enable press only re-reads + renders the card.
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            _u, text, _m = self._view(f"ctl:tasks:enable:{task_id}")
        upd.assert_not_called()
        self.assertIn("⚠️ تأكيد العملية", text)
        self.assertFalse(db.get_task(task_id)["active"])

        # The confirmation press calls the EXISTING contract.
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd2:
            _u2, text2, _m2 = self._view(
                f"ctl:tasks:confirm:enable:{task_id}"
            )
        upd2.assert_called_once_with(task_id, active=True)
        self.assertTrue(db.get_task(task_id)["active"])
        self.assertIn("⚡ الحالة: 🟢 مفعّلة", text2)

        # Disable goes through the same single contract.
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd3:
            self._view(f"ctl:tasks:disable:{task_id}")
            self._view(f"ctl:tasks:confirm:disable:{task_id}")
        upd3.assert_called_once_with(task_id, active=False)

        # No raw SQL exists in the admin module (existing guard),
        # and it never calls the store's create/delete directly.
        source = open(admin_control.__file__, encoding="utf-8").read()
        self.assertIsNone(_SQL_STATEMENT_RE.search(source))
        self.assertNotIn("db.create_task(", source)
        self.assertNotIn("db.delete_task(", source)

    def test_19_confirmation_is_required(self) -> None:
        """17. Every pre-confirmation surface writes NOTHING — the
        store and every table stay byte-identical."""
        off_id = self._seed_task("معطلة", active=False)
        on_id = self._seed_task("نشطة", active=True)
        before = self._dump_state()
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            for data in (
                "ctl:tasks",
                f"ctl:tasks:v:{off_id}",
                f"ctl:tasks:enable:{off_id}",
                f"ctl:tasks:disable:{on_id}",
                f"ctl:tasks:edit:{off_id}",
                f"ctl:tasks:field:title:{off_id}",
                "ctl:tasks:back",
                "ctl:refresh",
            ):
                self._press(data)
        upd.assert_not_called()
        self.assertEqual(before, self._dump_state())
        self.assertFalse(db.get_task(off_id)["active"])
        self.assertTrue(db.get_task(on_id)["active"])

    def test_20_enable_requires_confirmation_then_applies(self) -> None:
        """15 + 17. The enable press renders the confirmation card
        WITHOUT writing; only ✅ تأكيد applies it."""
        task_id = self._seed_task("مهمة معطلة", active=False)

        # Step 1: press enable → confirmation card, NO mutation.
        _u, text, markup = self._view(f"ctl:tasks:enable:{task_id}")
        self.assertIn("⚠️ تأكيد العملية", text)
        self.assertIn(f"المهمة: مهمة معطلة · #{task_id}", text)
        self.assertIn("العملية: تفعيل المهمة", text)
        self.assertIn("هل تريد المتابعة؟", text)
        payloads = self._button_payloads(markup)
        self.assertIn(f"ctl:tasks:confirm:enable:{task_id}", payloads)
        self.assertIn(f"ctl:tasks:cancel:{task_id}", payloads)
        self.assertFalse(db.get_task(task_id)["active"])  # no write!

        # Step 2: confirm → applies through db.update_task.
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            update = self._press(
                f"ctl:tasks:confirm:enable:{task_id}"
            )
        upd.assert_called_once_with(task_id, active=True)
        self.assertTrue(db.get_task(task_id)["active"])
        self.assertEqual(_answered(update.callback_query), TOAST_ENABLED)
        self.assertIn(
            "⚡ الحالة: 🟢 مفعّلة",
            _edited(update.callback_query),
        )

    def test_21_disable_preserves_history(self) -> None:
        """16 + 20. Disable flips only ``tasks.active`` — user-task
        history, submissions, reward and commission stay untouched."""
        user_id = 701
        self.assertTrue(db.register_user(user_id, "u701", "U701"))
        task_id = self._seed_task("مهمة نشطة", active=True)
        self.assertTrue(db.create_user_task(user_id, task_id))
        before_task = db.get_task(task_id)
        before = self._dump_state()

        # Step 1: the disable press only renders the card.
        _u, text, _m = self._view(f"ctl:tasks:disable:{task_id}")
        self.assertIn("العملية: تعطيل المهمة", text)
        self.assertTrue(db.get_task(task_id)["active"])  # still active!

        # Step 2: confirm → flips ONLY the active flag.
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            update = self._press(
                f"ctl:tasks:confirm:disable:{task_id}"
            )
        upd.assert_called_once_with(task_id, active=False)
        self.assertFalse(db.get_task(task_id)["active"])
        self.assertEqual(_answered(update.callback_query), TOAST_DISABLED)
        self.assertIn(
            "⚡ الحالة: 🔴 معطلة",
            _edited(update.callback_query),
        )

        after_task = db.get_task(task_id)
        after = self._dump_state()
        # History and every non-active field are untouched.
        self.assertEqual(before["user_tasks"], after["user_tasks"])
        self.assertEqual(
            before["task_submissions"], after["task_submissions"]
        )
        for table in (
            "wallets", "ledger", "withdrawal_requests",
            "deposit_requests", "payment_methods", "current_rate",
            "platform_settings",
        ):
            self.assertEqual(before[table], after[table], table)
        for field in (
            "title", "description", "type", "reward", "reward_units",
            "commission_units", "repeat_policy", "repeat_hours",
            "task_data", "created_at",
        ):
            self.assertEqual(
                before_task[field], after_task[field], field
            )
        self.assertNotEqual(before_task["active"], after_task["active"])

    def test_22_double_confirmation_never_double_applies(self) -> None:
        """18. A confirmation is single-use; pressing it twice never
        writes twice and the second press is a safe notice."""
        task_id = self._seed_task("مهمة", active=False)
        self._press(f"ctl:tasks:enable:{task_id}")  # render the card
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u1 = self._press(f"ctl:tasks:confirm:enable:{task_id}")
            u2 = self._press(f"ctl:tasks:confirm:enable:{task_id}")
        self.assertEqual(upd.call_count, 1)
        self.assertEqual(_answered(u1.callback_query), TOAST_ENABLED)
        self.assertIn("⚡ الحالة: 🟢 مفعّلة", _edited(u1.callback_query))
        self.assertEqual(_edited(u2.callback_query), MSG_STALE_TASK)
        self.assertIsNone(_answered(u2.callback_query))
        self.assertTrue(db.get_task(task_id)["active"])

    def test_23_state_race_re_read_blocks_mutation(self) -> None:
        """19. State can change between render and confirm — the
        handler re-reads immediately before writing and refuses to
        double-apply; vanished rows answer the fixed notice."""
        task_id = self._seed_task("مهمة", active=False)
        self._press(f"ctl:tasks:enable:{task_id}")  # card rendered

        # Another session flips the row before the confirm press.
        self.assertTrue(db.update_task(task_id, active=True))
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u = self._press(f"ctl:tasks:confirm:enable:{task_id}")
        upd.assert_not_called()  # fresh re-read saw matching state
        self.assertEqual(_edited(u.callback_query), MSG_STALE_TASK)
        self.assertIsNone(_answered(u.callback_query))

        # A render-time press for an already-matching state is stale.
        u2 = self._press(f"ctl:tasks:enable:{task_id}")
        self.assertEqual(_edited(u2.callback_query), MSG_STALE_TASK)

        # The row vanishes before the confirm press.
        self._press(f"ctl:tasks:disable:{task_id}")
        self.assertTrue(db.delete_task(task_id, db_path=self.db_path))
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd2:
            u3 = self._press(f"ctl:tasks:confirm:disable:{task_id}")
        upd2.assert_not_called()
        self.assertEqual(_edited(u3.callback_query), MSG_TASK_NOT_FOUND)


# ══════════════════════════════════════════════════════════════════
# F. EDIT FLOW — only the fields db.update_task supports (24-32)
# ══════════════════════════════════════════════════════════════════


class TestTasksEditFlow(TasksTestBase):

    def test_24_edit_menu_offers_only_supported_fields(self) -> None:
        """Allowed fields: title + description ONLY.  No reward, no
        type, no repeat policy, no delete anywhere in the menu."""
        task_id = self._seed_task("عنوان", "وصف")
        _u, text, markup = self._view(f"ctl:tasks:edit:{task_id}")
        self.assertIn("✏️ تعديل المهمة", text)
        self.assertIn("اختر الحقل المراد تعديله:", text)
        payloads = self._button_payloads(markup)
        self.assertEqual(
            payloads,
            [
                f"ctl:tasks:field:title:{task_id}",
                f"ctl:tasks:field:desc:{task_id}",
                f"ctl:tasks:v:{task_id}",
            ],
        )
        labels = self._labels(markup)
        self.assertIn("📌 العنوان", labels)
        self.assertIn("📝 الوصف", labels)
        for forbidden in ("reward", "delete", "type", "repeat"):
            for p in payloads:
                self.assertNotIn(forbidden, p)

        # Unknown task → fixed notice, no menu.
        _u2, text2, _m2 = self._view("ctl:tasks:edit:424242")
        self.assertEqual(text2, MSG_TASK_NOT_FOUND)

    def test_25_field_prompt_arms_pending_without_mutation(self) -> None:
        """Choosing a field stages ONE bounded pending edit in memory
        — no store write, no transaction, task row unchanged."""
        task_id = self._seed_task("عنوان قديم", "وصف قديم")
        before = self._dump_state()
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd, mock.patch.object(
            db, "get_task", wraps=db.get_task
        ) as getter:
            _u, text, markup = self._view(
                f"ctl:tasks:field:title:{task_id}"
            )
        upd.assert_not_called()
        getter.assert_called_once_with(task_id)  # authoritative read
        self.assertIn("أرسل النص الجديد", text)
        self.assertIn("عنوان المهمة", text)
        self.assertEqual(
            self._button_payloads(markup),
            [f"ctl:tasks:cancel:{task_id}"],
        )
        self.assertEqual(before, self._dump_state())
        pending = admin_control._PENDING_TASK_EDITS.get(ADMIN_ID)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["task_id"], task_id)
        self.assertEqual(pending["field"], "title")
        self.assertNotIn("value", pending)

        # Unknown task → fixed notice and NOTHING is armed.
        _u2, text2, _m2 = self._view("ctl:tasks:field:desc:424242")
        self.assertEqual(text2, MSG_TASK_NOT_FOUND)
        self.assertEqual(
            admin_control._PENDING_TASK_EDITS, {ADMIN_ID: pending}
        )

    def test_26_text_validation_rejects_then_stages(self) -> None:
        """The EXISTING task_taxonomy validators gate the input with
        their Arabic messages; the pending state survives an error so
        the admin can retry — and nothing is ever written here."""
        task_id = self._seed_task("عنوان قديم")
        self._press(f"ctl:tasks:field:title:{task_id}")
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u_empty = self._send_text("   ")
            u_multi = self._send_text("سطر أول\nسطر ثانٍ")
            u_long = self._send_text("x" * 201)
            # Snapshot AFTER the errors, BEFORE the valid staging.
            pending_after_errors = dict(
                admin_control._PENDING_TASK_EDITS[ADMIN_ID]
            )
            u_ok = self._send_text("عنوان جديد")
        upd.assert_not_called()  # staging never mutates
        self.assertEqual(
            _reply(u_empty), "❌ العنوان لا يمكن أن يكون فارغًا."
        )
        self.assertEqual(
            _reply(u_multi), "❌ العنوان يجب أن يكون في سطر واحد."
        )
        self.assertEqual(
            _reply(u_long), "❌ العنوان يتجاوز 200 حرفًا."
        )
        # Errors keep the pending edit (retry allowed), unstaged.
        pending = admin_control._PENDING_TASK_EDITS[ADMIN_ID]
        self.assertNotIn("value", pending_after_errors)
        self.assertEqual(db.get_task(task_id)["title"], "عنوان قديم")

        # Valid input → confirmation card with the reviewed value,
        # STILL without any mutation.
        card = _reply(u_ok)
        reply_markup = u_ok.message.reply_text.call_args[1]["reply_markup"]
        self.assertIn("⚠️ تأكيد العملية", card)
        self.assertIn("العملية: تعديل عنوان المهمة", card)
        self.assertIn("القيمة الجديدة:", card)
        self.assertIn("عنوان جديد", card)
        self.assertIn("هل تريد المتابعة؟", card)
        self.assertIn(
            f"ctl:tasks:confirm:edit:{task_id}",
            self._button_payloads(reply_markup),
        )
        self.assertEqual(pending["value"], "عنوان جديد")
        upd.assert_not_called()
        self.assertEqual(db.get_task(task_id)["title"], "عنوان قديم")

    def test_27_edit_confirm_applies_via_update_task(self) -> None:
        """Confirmation → the EXISTING db.update_task contract; a
        second confirmation finds no staged value (single-use)."""
        task_id = self._seed_task("عنوان قديم")
        self._press(f"ctl:tasks:field:title:{task_id}")
        self._send_text("عنوان جديد")
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u1 = self._press(f"ctl:tasks:confirm:edit:{task_id}")
            u2 = self._press(f"ctl:tasks:confirm:edit:{task_id}")
        upd.assert_called_once_with(task_id, title="عنوان جديد")
        self.assertEqual(_answered(u1.callback_query), TOAST_EDITED)
        self.assertIn(
            "📌 العنوان: عنوان جديد", _edited(u1.callback_query)
        )
        self.assertEqual(_edited(u2.callback_query), MSG_NO_PENDING)
        self.assertEqual(db.get_task(task_id)["title"], "عنوان جديد")
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_TASK_EDITS)

    def test_28_description_flow_allows_newlines(self) -> None:
        """The description field uses validate_instructions (newlines
        allowed) and delegates to the same single contract."""
        task_id = self._seed_task("مهمة", "وصف قديم")
        self._press(f"ctl:tasks:field:desc:{task_id}")
        u = self._send_text("سطر أول\nسطر ثانٍ")
        self.assertIn("سطر أول\nسطر ثانٍ", _reply(u))
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u1 = self._press(f"ctl:tasks:confirm:edit:{task_id}")
        upd.assert_called_once_with(
            task_id, description="سطر أول\nسطر ثانٍ"
        )
        self.assertEqual(_answered(u1.callback_query), TOAST_EDITED)
        self.assertEqual(
            db.get_task(task_id)["description"], "سطر أول\nسطر ثانٍ"
        )

    def test_29_cancel_clears_pending(self) -> None:
        """❌ إلغاء drops the staged edit, returns to the detail card
        and later texts stay silent."""
        task_id = self._seed_task("عنوان")
        self._press(f"ctl:tasks:field:title:{task_id}")
        u = self._press(f"ctl:tasks:cancel:{task_id}")
        self.assertEqual(_answered(u.callback_query), TOAST_CANCELLED)
        self.assertIn(TASK_DETAIL_HEADER, _edited(u.callback_query))
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_TASK_EDITS)

        u2 = self._send_text("نص متأخر")
        u2.message.reply_text.assert_not_called()
        self.assertEqual(db.get_task(task_id)["title"], "عنوان")

    def test_30_mismatched_or_fresh_confirm_is_rejected(self) -> None:
        """A confirm for a DIFFERENT task than the staged one is a
        safe notice and consumes the single-use pending state — the
        other task's data is never touched."""
        a = self._seed_task("أ", "و")
        b = self._seed_task("ب", "و")
        self._press(f"ctl:tasks:field:title:{a}")
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u1 = self._press(f"ctl:tasks:confirm:edit:{b}")
            u2 = self._press(f"ctl:tasks:confirm:edit:{a}")
        upd.assert_not_called()
        self.assertEqual(_edited(u1.callback_query), MSG_NO_PENDING)
        self.assertEqual(_edited(u2.callback_query), MSG_NO_PENDING)
        self.assertEqual(db.get_task(a)["title"], "أ")
        self.assertEqual(db.get_task(b)["title"], "ب")
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_TASK_EDITS)

    def test_31_missing_task_races_reply_safely(self) -> None:
        """The task vanishing BEFORE the text or BETWEEN staging and
        confirm → fixed notice, pending cleared, nothing written."""
        task_id = self._seed_task("مهمة")
        self._press(f"ctl:tasks:field:title:{task_id}")
        self.assertTrue(db.delete_task(task_id, db_path=self.db_path))
        u = self._send_text("نص جديد")
        self.assertEqual(_reply(u), MSG_TASK_NOT_FOUND)
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_TASK_EDITS)
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            u2 = self._press(f"ctl:tasks:confirm:edit:{task_id}")
        upd.assert_not_called()
        self.assertEqual(_edited(u2.callback_query), MSG_NO_PENDING)

        # Row vanishes AFTER the value was staged.
        tid2 = self._seed_task("مهمة أخرى")
        self._press(f"ctl:tasks:field:title:{tid2}")
        self._send_text("نص مؤجل")
        self.assertTrue(db.delete_task(tid2, db_path=self.db_path))
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd2:
            u3 = self._press(f"ctl:tasks:confirm:edit:{tid2}")
        upd2.assert_not_called()
        self.assertEqual(_edited(u3.callback_query), MSG_TASK_NOT_FOUND)

    def test_32_text_input_auth_and_isolation(self) -> None:
        """The text catch-all is silent without pending state, for
        non-admins (no validation, NO read), in groups, and for
        untrusted identities."""
        task_id = self._seed_task("مهمة")

        # No pending state → completely silent.
        u0 = self._send_text("نص عادي")
        u0.message.reply_text.assert_not_called()

        # Non-admin WITH pending state → silent before any read.
        admin_control._PENDING_TASK_EDITS[STRANGER] = {
            "task_id": task_id, "field": "title", "admin": STRANGER,
        }
        with mock.patch.object(
            db, "get_task", wraps=db.get_task
        ) as getter, mock.patch.object(
            task_taxonomy, "validate_title"
        ) as validator:
            u1 = self._send_text("محاولة", actor_id=STRANGER)
        getter.assert_not_called()
        validator.assert_not_called()
        u1.message.reply_text.assert_not_called()
        self.assertIn(STRANGER, admin_control._PENDING_TASK_EDITS)

        # Group chat → isolation first, still silent.
        group_actor = ADMIN_ID + 7
        admin_control._PENDING_TASK_EDITS[group_actor] = {
            "task_id": task_id, "field": "title", "admin": group_actor,
        }
        u2 = self._send_text(
            "نص في مجموعة", actor_id=group_actor, chat_type="supergroup"
        )
        u2.message.reply_text.assert_not_called()

        # Untrusted identity → silent.
        u3 = _update(ADMIN_ID, "نص")
        u3.effective_user.id = None
        _run(admin_control.task_edit_text_input(u3, mock.MagicMock()))
        u3.message.reply_text.assert_not_called()

        # Non-message update → silent.
        u4 = _update(ADMIN_ID, "نص")
        u4.message = None
        _run(admin_control.task_edit_text_input(u4, mock.MagicMock()))


# ══════════════════════════════════════════════════════════════════
# G. CREATE TASK — delegation only (33-34)
# ══════════════════════════════════════════════════════════════════


class TestTasksCreation(TasksTestBase):

    def test_33_new_delegates_to_existing_addtask_wizard(self) -> None:
        """``ctl:tasks:new`` presents the /addtask command to the
        EXISTING bot.add_task entry, which opens the canonical
        admin_task_wizard — one creation path, never two."""
        with mock.patch.object(
            admin_task_wizard, "start_wizard", new=mock.AsyncMock()
        ) as wizard:
            update = self._press("ctl:tasks:new")
        wizard.assert_awaited_once()
        shim, _ctx = wizard.await_args.args
        self.assertEqual(shim.message.text, "/addtask")
        self.assertEqual(shim.effective_user.id, ADMIN_ID)
        self.assertEqual(shim.effective_chat.type, "private")
        self.assertEqual(_answered(update.callback_query), BACK_HINT)

    def test_34_new_never_creates_directly_and_stale_press_fails(
        self,
    ) -> None:
        """The admin module never calls db.create_task itself, and a
        stale press (message gone) fails safely without delegating."""
        with mock.patch.object(
            admin_task_wizard, "start_wizard", new=mock.AsyncMock()
        ) as wizard, mock.patch.object(
            db, "create_task", wraps=db.create_task
        ) as create:
            self._press("ctl:tasks:new")
        wizard.assert_awaited_once()
        create.assert_not_called()

        with mock.patch.object(
            admin_task_wizard, "start_wizard", new=mock.AsyncMock()
        ) as wizard2:
            update = self._press("ctl:tasks:new", message_gone=True)
        wizard2.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)


# ══════════════════════════════════════════════════════════════════
# H. SAFETY — lifecycle, reward, financial tables (20-22)
# ══════════════════════════════════════════════════════════════════


class TestTasksSafety(TasksTestBase):

    def test_35_lifecycle_and_reward_semantics_remain_intact(
        self,
    ) -> None:
        """20 + 21. A full disable → enable round trip through the
        admin surface leaves EVERY table byte-identical (history,
        reward, commission, ledger all untouched), and the displayed
        reward still equals the persisted contract value."""
        user_id = 702
        self.assertTrue(db.register_user(user_id, "u702", "U702"))
        task_id = self._seed_task(
            "مهمة دورة", "وصف", "telegram", 3, active=True
        )
        self.assertTrue(db.create_user_task(user_id, task_id))
        before = self._dump_state()
        before_task = dict(db.get_task(task_id))

        self._press(f"ctl:tasks:disable:{task_id}")
        self._press(f"ctl:tasks:confirm:disable:{task_id}")
        self.assertFalse(db.get_task(task_id)["active"])
        self._press(f"ctl:tasks:enable:{task_id}")
        self._press(f"ctl:tasks:confirm:enable:{task_id}")

        after = self._dump_state()
        after_task = dict(db.get_task(task_id))
        self.assertTrue(after_task["active"])
        self.assertEqual(before, after)
        self.assertEqual(before_task, after_task)

        # Reward display = persisted whole-USDT contract value.
        _u, text, _m = self._view(f"ctl:tasks:v:{task_id}")
        self.assertIn("💰 المكافأة: 3 USDT", text)

    def test_35b_only_safe_update_fields_are_ever_written(
        self,
    ) -> None:
        """21. Across EVERY mutation flow, db.update_task never
        receives reward, reward_units, commission, type or repeat
        kwargs — the reward accounting contract is never bypassed."""
        on_id = self._seed_task("نشطة", active=True)
        off_id = self._seed_task("معطلة", active=False)
        with mock.patch.object(
            db, "update_task", wraps=db.update_task
        ) as upd:
            self._press(f"ctl:tasks:confirm:disable:{on_id}")
            self._press(f"ctl:tasks:confirm:enable:{off_id}")
            self._press(f"ctl:tasks:field:title:{on_id}")
            self._send_text("عنوان معدل")
            self._press(f"ctl:tasks:confirm:edit:{on_id}")
            self._press(f"ctl:tasks:field:desc:{off_id}")
            self._send_text("وصف معدل")
            self._press(f"ctl:tasks:confirm:edit:{off_id}")
        self.assertEqual(upd.call_count, 4)
        allowed = {"active", "title", "description"}
        forbidden = (
            "reward", "reward_units", "commission_units",
            "task_type", "repeat_policy", "repeat_hours", "db_path",
        )
        for call in upd.call_args_list:
            self.assertEqual(len(call.args), 1)  # task_id positional
            self.assertTrue(
                set(call.kwargs) <= allowed, f"unexpected write: {call}"
            )
            for name in forbidden:
                self.assertNotIn(name, call.kwargs)

    def test_36_read_paths_touch_no_financial_store_or_table(
        self,
    ) -> None:
        """22. Opening/refreshing/navigating the tasks surface never
        runs a financial mutation entry point, never opens a
        transaction and never changes a single table row."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        task_id = self._seed_task("مهمة")

        spies = {}
        for module, name in MUTATION_SPY_TARGETS:
            patcher = mock.patch.object(module, name)
            spies[f"{module.__name__}.{name}"] = patcher.start()
            self.addCleanup(patcher.stop)
        txn_patcher = mock.patch.object(db, "transaction")
        txn = txn_patcher.start()
        self.addCleanup(txn_patcher.stop)
        upd_patcher = mock.patch.object(db, "update_task")
        upd = upd_patcher.start()
        self.addCleanup(upd_patcher.stop)

        before = self._dump_state()
        self._cmd()
        for data in (
            "ctl:tasks",
            "ctl:tasks:p:0",
            "ctl:tasks:p:1",
            f"ctl:tasks:v:{task_id}",
            "ctl:tasks:v:999999",
            "ctl:tasks:back",
            f"ctl:tasks:edit:{task_id}",
            f"ctl:tasks:field:title:{task_id}",
            "ctl:refresh",
        ):
            self._press(data)

        txn.assert_not_called()
        upd.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())
        # The dump really covers the financial tables.
        for table in (
            "wallets",
            "ledger",
            "withdrawal_requests",
            "payment_methods",
            "deposit_requests",
            "deposit_proofs",
            "current_rate",
        ):
            self.assertIn(table, FINANCIAL_TABLES)


# ══════════════════════════════════════════════════════════════════
# I. REGISTRATION + NAVIGATION (23-24)
# ══════════════════════════════════════════════════════════════════


class TestTasksRegistration(unittest.TestCase):

    """23. No duplicate bot registration; bot.py untouched."""

    def test_37_no_static_registration_and_single_ctl_entry(
        self,
    ) -> None:
        """One ^ctl: CallbackQueryHandler (group 5), one /control
        CommandHandler (group 0), and NO static registration of the
        edit-text catch-all — it attaches lazily from admin_control."""
        captured, _bot_mod = _capture_handlers()

        ctl_handlers = [
            (h, g)
            for h, g in captured
            if isinstance(h, CallbackQueryHandler)
            and getattr(h, "pattern", None) is not None
            and h.pattern.pattern == r"^ctl:"
        ]
        self.assertEqual(len(ctl_handlers), 1, "^ctl: must stay unique")
        handler, group = ctl_handlers[0]
        self.assertIs(handler.callback, admin_control.control_callback)
        self.assertEqual(group, 5)

        control_handlers = [
            (h, g)
            for h, g in captured
            if isinstance(h, CommandHandler)
            and getattr(h, "commands", None)
            and "control" in h.commands
        ]
        self.assertEqual(len(control_handlers), 1)
        self.assertEqual(control_handlers[0][1], 0)

        for registered, _group in captured:
            self.assertIsNot(
                getattr(registered, "callback", None),
                admin_control.task_edit_text_input,
                "the edit-text catch-all must attach lazily, not here",
            )

        import bot as bot_mod

        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertNotIn("task_edit_text_input", source)
        self.assertEqual(source.count('pattern=r"^ctl:"'), 1)


class TestTasksLazyRegistration(TasksTestBase):

    """23. The lazy, idempotent edit-text handler attach."""

    def test_38_text_handler_attached_once_on_first_press(
        self,
    ) -> None:
        """The first ctl:tasks press attaches MessageHandler(group=3)
        ONCE (bot_data marker); later presses never double-register."""
        self._seed_task("مهمة")
        app = _FakeApplication()
        ctx = SimpleNamespace(application=app)
        for _ in range(2):
            update = _callback(ADMIN_ID, "ctl:tasks")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
        self.assertEqual(len(app.added), 1)
        handler, group = app.added[0]
        self.assertEqual(group, 3)
        self.assertIs(handler.callback, admin_control.task_edit_text_input)
        self.assertTrue(app.bot_data[admin_control._TEXT_HANDLER_MARK])

    def test_39_context_without_live_application_is_noop(self) -> None:
        """Unit-test shims (no Application / MagicMock bot_data)
        degrade to a no-op — the view still renders."""
        self._seed_task("مهمة")
        for ctx in (SimpleNamespace(), mock.MagicMock()):
            update = _callback(ADMIN_ID, "ctl:tasks")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
            update.callback_query.edit_message_text.assert_awaited_once()


class TestTasksNavigation(TasksTestBase):

    """24. Control Center navigation remains intact."""

    def test_40_registry_and_dashboard_entry_intact(self) -> None:
        """The frozen registry still owns the tasks key (rendered in
        place, no delegation command) and the dashboard still offers
        it alongside every other module."""
        expected_keys = {
            "users", "tasks", "reviews", "withdrawals", "deposits",
            "paymethods", "rate", "rewards", "broadcast", "support",
            "settings", "admins", "logs", "health",
        }
        self.assertEqual(set(admin_control.MODULES_BY_KEY), expected_keys)
        self.assertIsNone(admin_control.MODULES_BY_KEY["tasks"].command)
        self.assertIsNone(admin_control.MODULES_BY_KEY["users"].command)
        self.assertNotIn("tasks", admin_control._NAVIGATORS)
        self.assertIn("tasks", admin_control._KNOWN_OPS)

        payloads = self._button_payloads(build_dashboard_keyboard())
        self.assertIn("ctl:tasks", payloads)
        self.assertIn("ctl:users", payloads)

        update = self._cmd()
        self.assertIn(TASKS_HEADER, _reply(update))

    def test_41_other_modules_still_delegate(self) -> None:
        """Every other implemented module still routes to ITS
        existing command surface — nothing was moved."""
        import admin_review_queue
        import deposit_proof_admin
        import payment_method_admin
        import rate_admin
        import withdrawal_admin

        target_map = {
            "reviews": (admin_review_queue, "reviews_command"),
            "withdrawals": (withdrawal_admin, "withdrawals_command"),
            "deposits": (deposit_proof_admin, "deposits_command"),
            "paymethods": (payment_method_admin, "paymethods_command"),
            "rate": (rate_admin, "setrate_command"),
        }
        for key, (module, name) in target_map.items():
            with mock.patch.object(
                module, name, new=mock.AsyncMock()
            ) as target:
                self._press(f"ctl:{key}")
            target.assert_awaited_once()

    def test_42_back_refresh_and_users_grammar_route_correctly(
        self,
    ) -> None:
        """tasks:back returns to the panel, ctl:refresh still renders
        the dashboard, and the users sub-grammar still parses."""
        task_id = self._seed_task("مهمة")
        _u, _t, m = self._view(f"ctl:tasks:v:{task_id}")
        self.assertIn("ctl:tasks:back", self._button_payloads(m))

        _u2, t2, _m2 = self._view("ctl:tasks:back")
        self.assertIn(TASKS_PANEL_HEADER, t2)

        update = self._press("ctl:refresh")
        self.assertIn(HEADER, _edited(update.callback_query))
        update.callback_query.answer.assert_awaited()

        self.assertEqual(parse_callback("ctl:users:v:501"), "users:v:501")
        self.assertEqual(parse_callback("ctl:users"), "users")
        self.assertEqual(parse_callback("ctl:reviews"), "reviews")
        self.assertEqual(parse_callback("ctl:tasks"), "tasks")
        self.assertEqual(parse_callback("ctl:tasks:back"), "tasks:back")


class TestTasksCallbackGrammar(unittest.TestCase):

    """Parser-level guarantees for the tasks sub-grammar."""

    def test_canonicalization_and_bounds(self) -> None:
        # Canonical registry ops pass through unchanged.
        self.assertEqual(parse_callback("ctl:tasks"), "tasks")
        self.assertEqual(parse_callback("ctl:tasks:back"), "tasks:back")
        self.assertEqual(parse_callback("ctl:tasks:new"), "tasks:new")
        self.assertEqual(parse_callback("ctl:refresh"), "refresh")
        # Numeric payloads canonicalize to one spelling.
        self.assertEqual(parse_callback("ctl:tasks:p:007"), "tasks:p:7")
        self.assertEqual(parse_callback("ctl:tasks:p:0"), "tasks:p:0")
        self.assertEqual(parse_callback("ctl:tasks:v:00501"), "tasks:v:501")
        self.assertEqual(
            parse_callback("ctl:tasks:enable:009"), "tasks:enable:9"
        )
        self.assertEqual(
            parse_callback("ctl:tasks:disable:009"), "tasks:disable:9"
        )
        self.assertEqual(
            parse_callback("ctl:tasks:edit:009"), "tasks:edit:9"
        )
        self.assertEqual(
            parse_callback("ctl:tasks:cancel:009"), "tasks:cancel:9"
        )
        self.assertEqual(
            parse_callback("ctl:tasks:field:title:009"),
            "tasks:field:title:9",
        )
        self.assertEqual(
            parse_callback("ctl:tasks:field:desc:009"),
            "tasks:field:desc:9",
        )
        self.assertEqual(
            parse_callback("ctl:tasks:confirm:enable:009"),
            "tasks:confirm:enable:9",
        )
        self.assertEqual(
            parse_callback("ctl:tasks:confirm:disable:009"),
            "tasks:confirm:disable:9",
        )
        self.assertEqual(
            parse_callback("ctl:tasks:confirm:edit:009"),
            "tasks:confirm:edit:9",
        )
        # Out-of-grammar payloads fail safely.
        for bad in (
            "ctl:tasks:",
            "ctl:tasks:p:",
            "ctl:tasks:p:-1",
            "ctl:tasks:p:1.5",
            "ctl:tasks:p:" + "9" * 10,     # > 9 digits
            "ctl:tasks:v:" + "9" * 16,     # > 15 digits
            "ctl:tasks:v:abc",
            "ctl:tasks:field:reward:1",
            "ctl:tasks:confirm:reward:1",
            "ctl:tasks:confirm:delete:1",
            "ctl:tasks:new:1",
            "ctl:tasks;v:1",
            "ctl:tasksp:1",
            None,
            7,
            True,
        ):
            self.assertIsNone(parse_callback(bad), bad)

    def test_foreign_namespaces_untouched(self) -> None:
        """wd:/dp:/pm:/mr:/atw:/mproof:/sup: stay outside this
        grammar — the tasks parser never claims them."""
        for data in (
            "wd:x",
            "dp:x",
            "pm:x",
            "mr:view:1",
            "mr:vp:1",
            "atw:x",
            "mproof:x",
            "sup:x",
        ):
            self.assertIsNone(parse_callback(data), data)


if __name__ == "__main__":
    unittest.main()
