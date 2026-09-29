"""
Focused tests — Admin management & role control (MT-ADMIN-37)
==============================================================

The ``admins`` module of the Admin Control Center: an in-place
administrator-management surface (list → detail → confirmation →
store mutation) built on the persistent ``admin_users`` store
(``db.list_admin_users`` / ``db.get_admin_user`` /
``db.add_admin_user`` / ``db.remove_admin_user``) UNIONED with the
configured bootstrap list ``config.ADMINS`` — the exact union
``config.is_admin`` performs.  There is no second authorization
model: every entry re-checks ``config.is_admin`` first.

Coverage required by MT-ADMIN-37 §TESTS (brief number → test name):

 A. AUTHORIZATION (1-6)
   1   → test_01   non-admin cannot open the admin module
   2   → test_02   non-admin cannot list admins
   3   → test_03   non-admin cannot view admin detail
   4   → test_04   non-admin cannot add an admin
   5   → test_05   non-admin cannot remove an admin
   6   → test_06   group/channel access is silent

 B. LISTING (7-11)
   7   → test_07   list renders from the authoritative source
   8   → test_08   pagination works (deterministic order)
   9   → test_09   page clamping works; junk rejected pre-read
  10  → test_10   empty state works
  11  → test_11   stale/unknown target handled safely

 C. DETAIL (12-15)
  12  → test_12   existing admin detail (safe fields only)
  13  → test_13   missing admin fails safely
  14  → test_14   username missing → honest fallback
  15  → test_15   unavailable metadata uses ``غير متاح``

 D. ADD (16-24)
  16  → test_16   valid numeric Telegram ID end-to-end
  17  → test_17   zero rejected
  18  → test_18   negative rejected
  19  → test_19   malformed input rejected (unicode tricks too)
  20  → test_20   oversized ID rejected; 15-digit bound accepted
  21  → test_21   duplicate add is deterministic
  22  → test_22   confirmation required (staging never writes)
  23  → test_23   cancellation does not mutate
  24  → test_24   repeated confirmation is idempotent

 E. REMOVE (25-33)
  25  → test_25   valid removal (confirmation card → apply)
  26  → test_26   confirmation required (card never writes)
  27  → test_27   cancellation does not mutate
  28  → test_28   missing target fails safely
  29  → test_29   already removed → deterministic notice
  30  → test_30   final admin cannot be removed
  31  → test_31   self-removal safety (last refused, multi allowed)
  32  → test_32   repeated confirmation does not double-remove
  33  → test_33   concurrent/race-safe final-admin protection

 F. BOOTSTRAP (34-36)
  34  → test_34   configured/bootstrap admin remains authorized
  35  → test_35   database initialization is idempotent
  36  → test_36   no duplicate bootstrap rows
        → test_37   config.is_admin compatibility (union semantics)

 G. SAFETY (37-40)
  38-39 → test_38   read paths mutate nothing (no wallet/ledger/
        financial write, no transaction, no admin write)
        → test_39   authorized mutation flows touch ONLY
        admin_users — every financial table byte-identical
  40  → test_40   no secrets in rendered output or logs

 H. TEXT INPUT SECURITY (extra) → test_41..45
 I. REGISTRATION (extra)       → test_46..48  (bot.py untouched)
 J. NAVIGATION (extra)         → test_49..52
 K. GRAMMAR (extra)            → parser-level class

Temp databases only; no production destinations or balances used.

Run:
    .venv/bin/python -m pytest test_admin_admins.py -v
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import config
import db

from admin_control import (
    ADMIN_DETAIL_HEADER,
    ADMINS_PANEL_HEADER,
    HEADER,
    MSG_ADMIN_ALREADY_REMOVED,
    MSG_ADMIN_EXISTS,
    MSG_ADMIN_INVALID_ID,
    MSG_ADMIN_NOT_FOUND,
    MSG_ADMIN_ONLY,
    MSG_BOOTSTRAP_ADMIN,
    MSG_ERROR,
    MSG_INVALID,
    MSG_LAST_ADMIN,
    MSG_MODULE_UNAVAILABLE,
    MSG_NO_PENDING,
    TASKS_PANEL_HEADER,
    TOAST_ADMIN_ADDED,
    TOAST_ADMIN_REMOVED,
    TOAST_CANCELLED,
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
NA = "غير متاح"

# The configured bootstrap administrator captured BEFORE any fixture
# patches config.ADMINS (test modules import at collection time).
BOOTSTRAP_ID = config.ADMINS[0]

# Bot-token shape — must never appear in any rendered output.
_TOKEN_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}")
_FORBIDDEN_OUTPUT = (
    "secret", "password", "token", "initdata", "init_data",
    "minI_app".lower(), "task_coin", "rpc", "/home/", ".db",
)


class _FakeApplication:
    """Minimal Application stand-in: real dict bot_data + recorded
    add_handler calls (proves the lazy attach contract)."""

    def __init__(self):
        self.bot_data: dict = {}
        self.added: list = []

    def add_handler(self, handler, group=0):
        self.added.append((handler, group))


class AdminsTestBase(ControlTestBase):
    """MT-ADMIN-37 fixture: control-center drivers + admin seeding +
    admins-view helpers + pending-state hygiene.

    Fixture admin state: the temp DB carries the bootstrap row
    (synced by init_db BEFORE the fixture patches config.ADMINS), and
    ``config.ADMINS == [ADMIN_ID]`` — so the effective set is
    {BOOTSTRAP_ID (row), ADMIN_ID (configured, no row)} = 2."""

    def setUp(self):
        super().setUp()
        admin_control._PENDING_ADMIN_ADD.clear()
        self.addCleanup(admin_control._PENDING_ADMIN_ADD.clear)

    # ── seeds (existing store contract only) ────────────────────

    def _seed_admin(self, user_id: int, added_by: int | None = None) -> str:
        return db.add_admin_user(user_id, added_by=added_by,
                                 db_path=self.db_path)

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

    def _payloads(self, markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    def _send_text(self, text, actor_id: int = ADMIN_ID,
                   chat_type: str = "private"):
        update = _update(actor_id, text, chat_type=chat_type)
        _run(admin_control.admin_add_text_input(update, mock.MagicMock()))
        return update

    @contextmanager
    def _configured_admins(self, ids):
        """Temporarily replace the configured bootstrap list."""
        original = list(config.ADMINS)
        config.ADMINS[:] = list(ids)
        try:
            yield
        finally:
            config.ADMINS[:] = original

    @contextmanager
    def _db_spies(self):
        """Patch the four admin-store ops the module may use."""
        with mock.patch.object(
            db, "list_admin_users", wraps=db.list_admin_users
        ) as list_spy, mock.patch.object(
            db, "get_admin_user", wraps=db.get_admin_user
        ) as get_spy, mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy, mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as remove_spy:
            yield list_spy, get_spy, add_spy, remove_spy

    def _dump_admins(self) -> list:
        with db.get_connection(self.db_path) as conn:
            return [
                tuple(row)
                for row in conn.execute(
                    "SELECT user_id, active, created_at, added_by "
                    "FROM admin_users ORDER BY user_id"
                )
            ]

    @staticmethod
    def _assert_no_secrets(testcase, text: str) -> None:
        lowered = text.lower()
        for needle in _FORBIDDEN_OUTPUT:
            testcase.assertNotIn(needle, lowered, f"secret leak: {needle}")
        testcase.assertIsNone(_TOKEN_RE.search(text), "bot-token shape")


# ══════════════════════════════════════════════════════════════════
# A. AUTHORIZATION (1-6)
# ══════════════════════════════════════════════════════════════════


class TestAdminsAuthorization(AdminsTestBase):

    def test_01_non_admin_cannot_open_admin_module(self) -> None:
        """1. Non-admins get the standard refusal BEFORE any admin
        record is read."""
        with self._db_spies() as (list_spy, _g, _a, _r):
            update = self._press("ctl:admins", actor_id=STRANGER)
        list_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

    def test_02_non_admin_cannot_list_admins(self) -> None:
        """2. Pagination/list presses re-check authorization first."""
        for data in ("ctl:admins:p:0", "ctl:admins:p:1"):
            with self._db_spies() as (list_spy, _g, _a, _r):
                update = self._press(data, actor_id=STRANGER)
            list_spy.assert_not_called()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
            update.callback_query.edit_message_text.assert_not_called()

    def test_03_non_admin_cannot_view_detail(self) -> None:
        """3. Detail reads never run for a non-admin."""
        with self._db_spies() as (_l, get_spy, _a, _r):
            update = self._press(
                f"ctl:admins:v:{BOOTSTRAP_ID}", actor_id=STRANGER
            )
        get_spy.assert_not_called()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

    def test_04_non_admin_cannot_add_admin(self) -> None:
        """4. Every add entry (prompt, staged confirm) re-checks
        authorization before any read or write."""
        for data in (
            "ctl:admins:add",
            f"ctl:admins:confirm:add:{STRANGER}",
        ):
            with self._db_spies() as (l, g, add_spy, r):
                update = self._press(data, actor_id=STRANGER)
            l.assert_not_called()
            g.assert_not_called()
            add_spy.assert_not_called()
            r.assert_not_called()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
        self.assertEqual(admin_control._PENDING_ADMIN_ADD, {})

    def test_05_non_admin_cannot_remove_admin(self) -> None:
        """5. Every removal entry re-checks authorization before any
        read or write."""
        target = 7100001
        for data in (
            f"ctl:admins:remove:{target}",
            f"ctl:admins:confirm:remove:{target}",
            f"ctl:admins:cancel:{target}",
        ):
            with self._db_spies() as (l, g, a, remove_spy):
                update = self._press(data, actor_id=STRANGER)
            l.assert_not_called()
            g.assert_not_called()
            a.assert_not_called()
            remove_spy.assert_not_called()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
            update.callback_query.edit_message_text.assert_not_called()

    def test_06_group_and_channel_silent(self) -> None:
        """6. Group/channel invocations answer nothing and read
        nothing — MT-ADMIN-02 isolation."""
        for chat_type in ("supergroup", "channel"):
            for data in (
                "ctl:admins",
                "ctl:admins:add",
                f"ctl:admins:v:{BOOTSTRAP_ID}",
                f"ctl:admins:remove:{BOOTSTRAP_ID}",
            ):
                with self._db_spies() as (l, g, a, r):
                    update = self._press(data, chat_type=chat_type)
                l.assert_not_called()
                g.assert_not_called()
                a.assert_not_called()
                r.assert_not_called()
                self.assertIsNone(
                    _answered(update.callback_query), f"{chat_type} {data}"
                )
                update.callback_query.edit_message_text.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# B. LISTING (7-11)
# ══════════════════════════════════════════════════════════════════


class TestAdminsList(AdminsTestBase):

    def test_07_list_renders_from_authoritative_source(self) -> None:
        """7. The panel reads db.list_admin_users once (unioned with
        config) — no cache, no second query path."""
        with mock.patch.object(
            db, "list_admin_users", wraps=db.list_admin_users
        ) as src:
            _u, text, markup = self._view("ctl:admins")
        src.assert_called_once_with(active_only=True)
        self.assertIn(ADMINS_PANEL_HEADER, text)
        # Effective set = configured ADMIN_ID + bootstrap row.
        self.assertIn("👥 إجمالي المشرفين: 2", text)
        self.assertIn("صفحة 1/1", text)
        payloads = self._payloads(markup)
        self.assertIn(f"ctl:admins:v:{BOOTSTRAP_ID}", payloads)
        self.assertIn(f"ctl:admins:v:{ADMIN_ID}", payloads)
        self.assertIn("ctl:admins:add", payloads)
        self.assertIn("ctl:refresh", payloads)
        labels = self._labels(markup)
        self.assertTrue(
            any(l.startswith("👮") and f"🆔 {BOOTSTRAP_ID}" in l
                for l in labels),
            labels,
        )

    def test_08_pagination_is_bounded_and_deterministic(self) -> None:
        """8. Fixed page size, deterministic id order, prev/next —
        repeated renders are byte-identical."""
        for i in range(1000001, 1000008):
            self._seed_admin(i)
        # effective = 7 seeded + ADMIN_ID + bootstrap = 9 → 2 pages
        _u, text, markup = self._view("ctl:admins")
        self.assertIn("صفحة 1/2", text)
        page_rows = [
            p for p in self._payloads(markup) if p.startswith("ctl:admins:v:")
        ]
        self.assertEqual(
            page_rows,
            [
                f"ctl:admins:v:{ADMIN_ID}",          # 900240 sorts first
                "ctl:admins:v:1000001",
                "ctl:admins:v:1000002",
                "ctl:admins:v:1000003",
                "ctl:admins:v:1000004",
            ],
        )
        payloads = self._payloads(markup)
        self.assertIn("ctl:admins:p:1", payloads)
        self.assertNotIn("ctl:admins:p:0", payloads)  # no prev on page 1

        _u2, text2, markup2 = self._view("ctl:admins:p:1")
        self.assertIn("صفحة 2/2", text2)
        page_rows2 = [
            p for p in self._payloads(markup2)
            if p.startswith("ctl:admins:v:")
        ]
        self.assertEqual(
            page_rows2,
            [
                "ctl:admins:v:1000005",
                "ctl:admins:v:1000006",
                "ctl:admins:v:1000007",
                f"ctl:admins:v:{BOOTSTRAP_ID}",
            ],
        )
        self.assertIn("ctl:admins:p:0", self._payloads(markup2))

        # Determinism: identical payloads on a repeat render.
        _u3, _t3, m3 = self._view("ctl:admins")
        self.assertEqual(self._payloads(m3), payloads)

    def test_09_page_clamped_and_junk_rejected(self) -> None:
        """9. Oversized-but-valid page → clamped to the real last
        page; junk page values never reach the store."""
        self._seed_admin(1000001)
        _u, text, _m = self._view("ctl:admins:p:999")
        self.assertIn("صفحة 1/1", text)  # clamped, not an error

        with self._db_spies() as (list_spy, _g, _a, _r):
            update = self._press("ctl:admins:p:" + "9" * 10)  # 10 digits
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)
        list_spy.assert_not_called()
        update.callback_query.edit_message_text.assert_not_called()

    def test_10_empty_state_renders_safely(self) -> None:
        """10. Zero effective admins → stable empty page (builders
        stay total: clamp + notice, never a crash)."""
        self.assertEqual(
            db.remove_admin_user(BOOTSTRAP_ID, db_path=self.db_path),
            "removed",
        )
        with self._configured_admins([]):
            view = admin_control.collect_admins_page(0)
            self.assertEqual(view["total"], 0)
            self.assertEqual(view["rows"], [])
            text = admin_control.build_admins_text(view)
            self.assertIn("📭 لا يوجد مشرفون.", text)
            self.assertIn("صفحة 1/1", text)
            # Page index clamped even when oversized.
            self.assertEqual(admin_control.collect_admins_page(5)["page"], 0)

    def test_11_stale_and_unknown_targets_fail_safely(self) -> None:
        """11. Stale/unknown ids answer the fixed notice — no crash,
        no traceback, no database detail."""
        unknown = 999999999999  # in-grammar, nonexistent
        _u, text, markup = self._view(f"ctl:admins:v:{unknown}")
        self.assertEqual(text, MSG_ADMIN_NOT_FOUND)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("sqlite", text.lower())
        self.assertEqual(self._payloads(markup), ["ctl:admins:back"])

        _u2, text2, _m2 = self._view(f"ctl:admins:remove:{unknown}")
        self.assertEqual(text2, MSG_ADMIN_NOT_FOUND)


# ══════════════════════════════════════════════════════════════════
# C. DETAIL (12-15)
# ══════════════════════════════════════════════════════════════════


class TestAdminsDetail(AdminsTestBase):

    def test_12_existing_admin_detail_with_safe_fields(self) -> None:
        """12. Every card value comes from the authoritative row +
        the identity-only users lookup."""
        self.assertTrue(db.register_user(7400001, "admin_user", "Admin Name"))
        self._seed_admin(7400001, added_by=ADMIN_ID)
        _u, text, markup = self._view(f"ctl:admins:v:7400001")
        self.assertIn(ADMIN_DETAIL_HEADER, text)
        self.assertIn("🆔 Telegram ID: 7400001", text)
        self.assertIn("👤 Username: @admin_user", text)
        self.assertIn("📛 الاسم: Admin Name", text)
        self.assertRegex(text, r"📅 تمت الإضافة: \d{4}-\d{2}-\d{2}")
        self.assertIn(f"👤 أضيف بواسطة: {ADMIN_ID}", text)
        self.assertIn("🟢 الحالة: فعال", text)
        payloads = self._payloads(markup)
        self.assertIn("ctl:admins:remove:7400001", payloads)
        self.assertIn("ctl:admins:back", payloads)
        # Detail exposes ONLY the documented labels.
        allowed = ("🆔", "👤", "📛", "📅", "🟢", "🔴")
        for line in text.splitlines():
            if not line or line == ADMIN_DETAIL_HEADER:
                continue
            self.assertTrue(
                line.startswith(allowed), f"unexpected line: {line!r}"
            )

    def test_13_missing_admin_fails_safely(self) -> None:
        """13. Unknown id → fixed notice; never a traceback or SQL."""
        _u, text, _m = self._view("ctl:admins:v:424242424")
        self.assertEqual(text, MSG_ADMIN_NOT_FOUND)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("SELECT", text)

    def test_14_username_missing_uses_honest_fallback(self) -> None:
        """14. An admin who never registered a username shows the
        fixed fallback — nothing is fabricated."""
        self._seed_admin(7500001)  # no users row at all
        _u, text, _m = self._view("ctl:admins:v:7500001")
        self.assertIn(f"👤 Username: {NA}", text)
        self.assertIn(f"📛 الاسم: {NA}", text)
        self.assertNotIn("@", text.split("Username:")[1].splitlines()[0])

        _u2, _t2, m2 = self._view("ctl:admins")
        labels = self._labels(m2)
        self.assertTrue(
            any("بدون اسم مستخدم" in l and "7500001" in l for l in labels),
            labels,
        )

    def test_15_unavailable_metadata_uses_na(self) -> None:
        """15. The configured bootstrap record (no row in this
        fixture) renders with ``غير متاح`` metadata — never
        fabricated timestamps — and offers no remove button."""
        self.assertNotIn(
            ADMIN_ID,
            [r["user_id"] for r in db.list_admin_users(db_path=self.db_path)],
        )
        _u, text, markup = self._view(f"ctl:admins:v:{ADMIN_ID}")
        self.assertIn(ADMIN_DETAIL_HEADER, text)
        self.assertIn(f"🆔 Telegram ID: {ADMIN_ID}", text)
        self.assertIn(f"📅 تمت الإضافة: {NA}", text)
        self.assertIn(f"👤 أضيف بواسطة: {NA}", text)
        self.assertIn("🟢 الحالة: فعال", text)
        payloads = self._payloads(markup)
        self.assertNotIn(f"ctl:admins:remove:{ADMIN_ID}", payloads)
        self.assertIn("ctl:admins:back", payloads)


# ══════════════════════════════════════════════════════════════════
# D. ADD (16-24)
# ══════════════════════════════════════════════════════════════════


class TestAdminsAdd(AdminsTestBase):

    def _stage(self, raw: str):
        """Prompt → text (returns the staged reply update)."""
        self._press("ctl:admins:add")
        return self._send_text(raw)

    def test_16_valid_numeric_id_end_to_end(self) -> None:
        """16. digits → confirmation card → confirm → exactly one
        persisted row with audit metadata; the new admin authorizes
        through the SAME centralized config.is_admin."""
        target = 123456789
        self.assertFalse(config.is_admin(target))

        # Prompt arms a pending input — no mutation.
        _u, text, _m = self._view("ctl:admins:add")
        self.assertIn("أرسل Telegram ID للمشرف الجديد", text)
        self.assertIn("لا يُقبل @username", text)
        self.assertEqual(
            admin_control._PENDING_ADMIN_ADD[ADMIN_ID], {"admin": ADMIN_ID}
        )
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            u_reply = self._send_text(str(target))
            add_spy.assert_not_called()  # staging never writes
        card = _reply(u_reply)
        self.assertIn("⚠️ تأكيد إضافة مشرف", card)
        self.assertIn(f"🆔 Telegram ID: {target}", card)
        self.assertIn("👤 Username: بدون اسم مستخدم", card)
        self.assertIn("هل تريد المتابعة؟", card)
        reply_markup = u_reply.message.reply_text.call_args[1]["reply_markup"]
        self.assertIn(
            f"ctl:admins:confirm:add:{target}", self._payloads(reply_markup)
        )

        # Confirm → ONE authoritative write + refreshed list.
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            u = self._press(f"ctl:admins:confirm:add:{target}")
        add_spy.assert_called_once_with(target, added_by=ADMIN_ID)
        self.assertEqual(_answered(u.callback_query), TOAST_ADMIN_ADDED)
        self.assertIn(ADMINS_PANEL_HEADER, _edited(u.callback_query))

        row = db.get_admin_user(target)
        self.assertIsNotNone(row)
        self.assertEqual(row["active"], 1)
        self.assertEqual(row["added_by"], ADMIN_ID)
        self.assertTrue(config.is_admin(target))
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_ADMIN_ADD)

    def test_17_zero_rejected(self) -> None:
        """17. Zero (and all-zero strings) are rejected before any
        read or write; the pending state survives for a retry."""
        self._press("ctl:admins:add")
        for raw in ("0", "000"):
            u = self._send_text(raw)
            self.assertEqual(_reply(u), MSG_ADMIN_INVALID_ID, raw)
        self.assertIsNone(db.get_admin_user(0, db_path=self.db_path))
        pending = admin_control._PENDING_ADMIN_ADD[ADMIN_ID]
        self.assertNotIn("value", pending)  # nothing staged

    def test_18_negative_rejected(self) -> None:
        """18. Negative values never canonicalize."""
        self._press("ctl:admins:add")
        for raw in ("-5", "-900240", " -7 "):
            u = self._send_text(raw)
            self.assertEqual(_reply(u), MSG_ADMIN_INVALID_ID, raw)
        self.assertNotIn("value", admin_control._PENDING_ADMIN_ADD[ADMIN_ID])

    def test_19_malformed_input_rejected(self) -> None:
        """19. Non-numeric input — including Arabic-Indic digit
        tricks, decimals, signs, @usernames and embedded spaces — is
        rejected by the strict ASCII pattern; a retry then works."""
        self._press("ctl:admins:add")
        for raw in (
            "abc", "", "12a", "1 2", "@someuser", "١٢٣", "12.5",
            "+5", "0x10", "1\n2", "inf", "١٢٣٤٥",
        ):
            u = self._send_text(raw)
            self.assertEqual(_reply(u), MSG_ADMIN_INVALID_ID, raw)
        self.assertNotIn("value", admin_control._PENDING_ADMIN_ADD[ADMIN_ID])
        self.assertIsNone(db.get_admin_user(12, db_path=self.db_path))

        # Pending survived the errors — a valid id now stages.
        u = self._send_text("7300001")
        self.assertIn("⚠️ تأكيد إضافة مشرف", _reply(u))
        self.assertEqual(
            admin_control._PENDING_ADMIN_ADD[ADMIN_ID]["value"], 7300001
        )

    def test_20_oversized_rejected_bound_accepted(self) -> None:
        """20. Beyond the 15-digit bound → rejected; exactly 15
        digits canonicalizes fine (same bound as the grammar)."""
        self._press("ctl:admins:add")
        u = self._send_text("9" * 16)
        self.assertEqual(_reply(u), MSG_ADMIN_INVALID_ID)
        u2 = self._send_text("1" * 15)
        self.assertIn(
            f"🆔 Telegram ID: {'1' * 15}", _reply(u2)
        )
        self.assertEqual(
            admin_control._PENDING_ADMIN_ADD[ADMIN_ID]["value"], int("1" * 15)
        )

    def test_21_duplicate_add_is_deterministic(self) -> None:
        """21. An already-active target (store row or configured
        list) answers the SAME fixed message and writes nothing."""
        self._seed_admin(7600001, added_by=BOOTSTRAP_ID)
        before = self._dump_admins()
        self._stage(str(7600001))
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            u = self._press("ctl:admins:confirm:add:7600001")
        add_spy.assert_not_called()
        self.assertEqual(_edited(u.callback_query), MSG_ADMIN_EXISTS)
        self.assertEqual(before, self._dump_admins())
        rows = [
            r for r in db.list_admin_users(db_path=self.db_path)
            if r["user_id"] == 7600001
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["added_by"], BOOTSTRAP_ID)

        # Configured (bootstrap) target → the SAME deterministic text.
        self._stage(str(ADMIN_ID))
        u2 = self._press(f"ctl:admins:confirm:add:{ADMIN_ID}")
        self.assertEqual(_edited(u2.callback_query), MSG_ADMIN_EXISTS)
        self.assertEqual(before, self._dump_admins())

    def test_22_confirmation_is_required(self) -> None:
        """22. Prompt and staging write NOTHING — only the ✅ تأكيد
        confirmation performs the single persist."""
        target = 7610001
        before = self._dump_admins()
        self._press("ctl:admins:add")
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            self._send_text(str(target))
        add_spy.assert_not_called()
        self.assertEqual(before, self._dump_admins())  # still identical
        self.assertIsNone(db.get_admin_user(target, db_path=self.db_path))

        self._press(f"ctl:admins:confirm:add:{target}")
        self.assertEqual(db.get_admin_user(target)["active"], 1)

    def test_23_cancellation_does_not_mutate(self) -> None:
        """23. Cancelling from the prompt OR from the confirmation
        card clears the single-use pending state and writes nothing."""
        target = 7620001
        before = self._dump_admins()

        # Cancel from the prompt (bare back op doubles as cancel).
        self._press("ctl:admins:add")
        u = self._press("ctl:admins:back")
        self.assertIn(ADMINS_PANEL_HEADER, _edited(u.callback_query))
        self.assertNotIn(ADMIN_ID, admin_control._PENDING_ADMIN_ADD)
        silent = self._send_text(str(target))  # no pending → silent
        silent.message.reply_text.assert_not_called()
        self.assertEqual(before, self._dump_admins())

        # Cancel from the confirmation card.
        self._press("ctl:admins:add")
        self._send_text(str(target))
        u2 = self._press("ctl:admins:back")
        self.assertIn(ADMINS_PANEL_HEADER, _edited(u2.callback_query))
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            u3 = self._press(f"ctl:admins:confirm:add:{target}")
        add_spy.assert_not_called()
        self.assertEqual(_edited(u3.callback_query), MSG_NO_PENDING)
        self.assertEqual(before, self._dump_admins())
        self.assertIsNone(db.get_admin_user(target, db_path=self.db_path))

    def test_24_repeated_confirmation_is_idempotent(self) -> None:
        """24. A second confirmation finds no staged value (single
        use) and the store itself is duplicate-proof."""
        target = 7630001
        self._stage(str(target))
        with mock.patch.object(
            db, "add_admin_user", wraps=db.add_admin_user
        ) as add_spy:
            u1 = self._press(f"ctl:admins:confirm:add:{target}")
            u2 = self._press(f"ctl:admins:confirm:add:{target}")
        self.assertEqual(add_spy.call_count, 1)
        self.assertEqual(_answered(u1.callback_query), TOAST_ADMIN_ADDED)
        self.assertEqual(_edited(u2.callback_query), MSG_NO_PENDING)
        rows = [
            r for r in db.list_admin_users(db_path=self.db_path)
            if r["user_id"] == target
        ]
        self.assertEqual(len(rows), 1)  # never duplicated


# ══════════════════════════════════════════════════════════════════
# E. REMOVE (25-33)
# ══════════════════════════════════════════════════════════════════


class TestAdminsRemove(AdminsTestBase):

    def test_25_valid_removal_end_to_end(self) -> None:
        """25. Card (read-only) → confirm → soft-remove via the
        single store operation → refreshed list without the target."""
        target = 7700001
        self._seed_admin(target)
        before = self._dump_admins()

        _u, text, markup = self._view(f"ctl:admins:remove:{target}")
        self.assertIn("⚠️ تأكيد إزالة المشرف", text)
        self.assertIn(f"🆔 Telegram ID: {target}", text)
        self.assertIn("👤 Username: بدون اسم مستخدم", text)
        self.assertIn("هل أنت متأكد من إزالة هذا المشرف؟", text)
        self.assertEqual(
            self._payloads(markup),
            [f"ctl:admins:confirm:remove:{target}",
             f"ctl:admins:cancel:{target}"],
        )
        self.assertEqual(before, self._dump_admins())  # card wrote nothing

        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            u = self._press(f"ctl:admins:confirm:remove:{target}")
        rm.assert_called_once_with(target)
        self.assertEqual(_answered(u.callback_query), TOAST_ADMIN_REMOVED)
        edited = _edited(u.callback_query)
        self.assertIn(ADMINS_PANEL_HEADER, edited)
        refreshed = u.callback_query.edit_message_text.call_args[1][
            "reply_markup"
        ]
        self.assertNotIn(
            f"ctl:admins:v:{target}", self._payloads(refreshed)
        )
        row = db.get_admin_user(target)
        self.assertEqual(row["active"], 0)  # soft state — history kept
        self.assertFalse(config.is_admin(target))

    def test_26_confirmation_is_required(self) -> None:
        """26. The remove press renders a card and NEVER writes."""
        target = 7710001
        self._seed_admin(target)
        before = self._dump_admins()
        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            self._press(f"ctl:admins:remove:{target}")
        rm.assert_not_called()
        self.assertEqual(before, self._dump_admins())
        self.assertEqual(db.get_admin_user(target)["active"], 1)

    def test_27_cancellation_does_not_mutate(self) -> None:
        """27. ❌ إلغاء returns to the detail card and writes
        nothing."""
        target = 7720001
        self._seed_admin(target)
        before = self._dump_admins()
        self._press(f"ctl:admins:remove:{target}")
        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            u = self._press(f"ctl:admins:cancel:{target}")
        rm.assert_not_called()
        self.assertEqual(_answered(u.callback_query), TOAST_CANCELLED)
        self.assertIn(ADMIN_DETAIL_HEADER, _edited(u.callback_query))
        self.assertEqual(before, self._dump_admins())
        self.assertEqual(db.get_admin_user(target)["active"], 1)

    def test_28_missing_target_fails_safely(self) -> None:
        """28. A target with no row at all → fixed not-found at the
        press AND at the confirm; nothing is written."""
        unknown = 773000999
        _u, text, _m = self._view(f"ctl:admins:remove:{unknown}")
        self.assertEqual(text, MSG_ADMIN_NOT_FOUND)
        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            u = self._press(f"ctl:admins:confirm:remove:{unknown}")
        rm.assert_not_called()
        self.assertEqual(_edited(u.callback_query), MSG_ADMIN_NOT_FOUND)

    def test_29_already_removed_is_deterministic(self) -> None:
        """29. An inactive row answers the SAME fixed message at the
        press and at the confirm — never a second removal."""
        target = 7740001
        self._seed_admin(target)
        self.assertEqual(db.remove_admin_user(target), "removed")
        before = self._dump_admins()

        _u, text, _m = self._view(f"ctl:admins:remove:{target}")
        self.assertEqual(text, MSG_ADMIN_ALREADY_REMOVED)
        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            u = self._press(f"ctl:admins:confirm:remove:{target}")
        rm.assert_not_called()
        self.assertEqual(_edited(u.callback_query), MSG_ADMIN_ALREADY_REMOVED)
        self.assertEqual(before, self._dump_admins())

    def test_30_final_admin_cannot_be_removed(self) -> None:
        """30. With exactly ONE effective administrator left, the
        confirm refuses with the fixed lockout-protection notice."""
        self.assertEqual(db.remove_admin_user(BOOTSTRAP_ID), "removed")
        self.assertEqual(self._seed_admin(ADMIN_ID), "created")
        with self._configured_admins([]):
            self.assertEqual(
                admin_control._effective_admin_ids(), {ADMIN_ID}
            )
            self.assertTrue(config.is_admin(ADMIN_ID))  # store half
            u = self._press(f"ctl:admins:confirm:remove:{ADMIN_ID}")
            self.assertEqual(_edited(u.callback_query), MSG_LAST_ADMIN)
            self.assertEqual(db.get_admin_user(ADMIN_ID)["active"], 1)
            self.assertTrue(config.is_admin(ADMIN_ID))

    def test_31_self_removal_safety(self) -> None:
        """31. Self-removal is refused while it would leave nobody,
        and allowed only when the resulting state stays valid."""
        self.assertEqual(db.remove_admin_user(BOOTSTRAP_ID), "removed")
        self.assertEqual(self._seed_admin(ADMIN_ID), "created")
        with self._configured_admins([]):
            # a) Self as the ONLY admin → refused.
            u1 = self._press(f"ctl:admins:confirm:remove:{ADMIN_ID}")
            self.assertEqual(_edited(u1.callback_query), MSG_LAST_ADMIN)
            self.assertEqual(db.get_admin_user(ADMIN_ID)["active"], 1)

            # b) Another active admin exists → self-removal allowed
            #    and the remaining state is still fully valid.
            self._seed_admin(7750001)
            u2 = self._press(f"ctl:admins:confirm:remove:{ADMIN_ID}")
            self.assertEqual(_answered(u2.callback_query), TOAST_ADMIN_REMOVED)
            self.assertEqual(db.get_admin_user(ADMIN_ID)["active"], 0)
            self.assertFalse(config.is_admin(ADMIN_ID))
            self.assertEqual(db.get_admin_user(7750001)["active"], 1)
            self.assertTrue(config.is_admin(7750001))  # successor admin

    def test_32_repeated_confirmation_does_not_double_remove(
        self,
    ) -> None:
        """32. A second confirmation is a safe notice and the store
        never flips twice."""
        target = 7760001
        self._seed_admin(target)
        with mock.patch.object(
            db, "remove_admin_user", wraps=db.remove_admin_user
        ) as rm:
            u1 = self._press(f"ctl:admins:confirm:remove:{target}")
            after_first = self._dump_admins()
            u2 = self._press(f"ctl:admins:confirm:remove:{target}")
        self.assertEqual(rm.call_count, 1)  # 2nd re-read refused early
        self.assertEqual(_answered(u1.callback_query), TOAST_ADMIN_REMOVED)
        self.assertEqual(_edited(u2.callback_query), MSG_ADMIN_ALREADY_REMOVED)
        self.assertEqual(after_first, self._dump_admins())  # no 2nd write

    def test_33_race_safe_final_admin_protection(self) -> None:
        """33. State changing BETWEEN render and confirm is re-read:
        a removal card rendered while two admins remained must be
        refused once concurrent removals leave the target as the
        final administrator — never a lockout."""
        self.assertEqual(db.remove_admin_user(BOOTSTRAP_ID), "removed")
        self.assertEqual(self._seed_admin(ADMIN_ID), "created")
        self._seed_admin(7770002)
        with self._configured_admins([]):
            # effective = {ADMIN_ID, 7770002} = 2 → card renders.
            _u, text, _m = self._view(f"ctl:admins:remove:{ADMIN_ID}")
            self.assertIn("⚠️ تأكيد إزالة المشرف", text)
            self.assertEqual(
                admin_control._effective_admin_ids(),
                {ADMIN_ID, 7770002},
            )

            # Concurrent session removes the other administrator,
            # leaving the target as the FINAL one.
            self.assertEqual(db.remove_admin_user(7770002), "removed")
            self.assertEqual(
                admin_control._effective_admin_ids(), {ADMIN_ID}
            )

            # The stale confirm re-reads the fresh count → refused.
            with mock.patch.object(
                db, "remove_admin_user", wraps=db.remove_admin_user
            ) as rm:
                u = self._press(
                    f"ctl:admins:confirm:remove:{ADMIN_ID}"
                )
            rm.assert_not_called()
            self.assertEqual(_edited(u.callback_query), MSG_LAST_ADMIN)
            self.assertEqual(db.get_admin_user(ADMIN_ID)["active"], 1)
            self.assertTrue(config.is_admin(ADMIN_ID))


# ══════════════════════════════════════════════════════════════════
# F. BOOTSTRAP (34-37)
# ══════════════════════════════════════════════════════════════════


class TestAdminsBootstrap(AdminsTestBase):

    def test_34_configured_bootstrap_remains_authorized(self) -> None:
        """34. The configured administrator stays authorized even if
        the database row disappears — config.is_admin checks the
        configured list FIRST — and stays visible via synthesis."""
        with self._configured_admins([ADMIN_ID, BOOTSTRAP_ID]):
            self.assertEqual(
                db.remove_admin_user(BOOTSTRAP_ID, db_path=self.db_path),
                "removed",
            )
            self.assertTrue(config.is_admin(BOOTSTRAP_ID))  # config first
            # Visible in the surface despite the absent active row.
            resolved = admin_control._resolve_admin(BOOTSTRAP_ID)
            self.assertIsNotNone(resolved)
            self.assertEqual(
                admin_control._effective_admin_ids(),
                {ADMIN_ID, BOOTSTRAP_ID},
            )
            # Deterministic re-sync on init — row restored, never dup.
            db.init_db(self.db_path)
            rows = [
                r for r in db.list_admin_users(db_path=self.db_path)
                if r["user_id"] == BOOTSTRAP_ID
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["active"], 1)
            # Removing it from Telegram is refused — no lockout path.
            u = self._press(
                f"ctl:admins:confirm:remove:{BOOTSTRAP_ID}"
            )
            self.assertEqual(_edited(u.callback_query), MSG_BOOTSTRAP_ADMIN)

    def test_35_database_initialization_is_idempotent(self) -> None:
        """35. init_db() twice → byte-identical rows; a manually
        added admin survives re-init without duplication."""
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = handle.name

        def _cleanup():
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

        self.addCleanup(_cleanup)
        db.init_db(path)
        first = db.list_admin_users(db_path=path)
        db.init_db(path)
        self.assertEqual(first, db.list_admin_users(db_path=path))

        db.add_admin_user(777111, added_by=BOOTSTRAP_ID, db_path=path)
        db.init_db(path)
        rows = [
            r for r in db.list_admin_users(db_path=path)
            if r["user_id"] == 777111
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active"], 1)

    def test_36_no_duplicate_bootstrap_rows(self) -> None:
        """36. Each configured id owns exactly ONE row after any
        number of initializations."""
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        path = handle.name

        def _cleanup():
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

        self.addCleanup(_cleanup)
        with self._configured_admins([ADMIN_ID, BOOTSTRAP_ID]):
            db.init_db(path)
            db.init_db(path)
            for admin_id in (ADMIN_ID, BOOTSTRAP_ID):
                rows = [
                    r for r in db.list_admin_users(db_path=path)
                    if r["user_id"] == admin_id
                ]
                self.assertEqual(len(rows), 1, admin_id)

    def test_37_config_is_admin_compatibility(self) -> None:
        """37 (extra). The public contract is unchanged: configured
        admin True, store admin True, strangers and malformed
        identities False — one decision, never a second model."""
        self.assertTrue(config.is_admin(ADMIN_ID))        # configured
        self.assertTrue(config.is_admin(BOOTSTRAP_ID))    # store row
        self.assertFalse(config.is_admin(STRANGER))
        for bad in (True, False, "900240", 0, -1, None, 3.14, (1,)):
            self.assertFalse(config.is_admin(bad), repr(bad))

        # A store-backed admin authorizes through the same call.
        self._seed_admin(7800001)
        self.assertTrue(config.is_admin(7800001))
        db.remove_admin_user(7800001, db_path=self.db_path)
        self.assertFalse(config.is_admin(7800001))


# ══════════════════════════════════════════════════════════════════
# G. SAFETY (38-40)
# ══════════════════════════════════════════════════════════════════


class TestAdminsSafety(AdminsTestBase):

    def test_38_read_paths_write_nothing(self) -> None:
        """38 + 39 (read side). Opening/refreshing/navigating the
        admins surface runs no financial mutation entry point, opens
        no transaction and changes NO table row — financial or
        admin."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        target = 7900001
        self._seed_admin(target)

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

        fin_before = self._dump_state()
        adm_before = self._dump_admins()
        self._cmd()
        for data in (
            "ctl:admins",
            "ctl:admins:p:0",
            "ctl:admins:p:1",
            f"ctl:admins:v:{target}",
            f"ctl:admins:v:{BOOTSTRAP_ID}",
            "ctl:admins:v:999999999999",
            "ctl:admins:add",
            f"ctl:admins:remove:{target}",
            f"ctl:admins:cancel:{target}",
            "ctl:admins:back",
            "ctl:refresh",
        ):
            self._press(data)

        txn.assert_not_called()
        upd.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(fin_before, self._dump_state())
        self.assertEqual(adm_before, self._dump_admins())
        for table in (
            "wallets", "ledger", "withdrawal_requests",
            "payment_methods", "deposit_requests", "deposit_proofs",
            "current_rate",
        ):
            self.assertIn(table, FINANCIAL_TABLES)

    def test_39_mutation_flows_touch_only_admin_users(self) -> None:
        """39 (write side). Even the AUTHORIZED add + remove flows
        change exactly one table — admin_users — while every
        financial table stays byte-identical and no financial entry
        point runs."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)

        spies = {}
        for module, name in MUTATION_SPY_TARGETS:
            patcher = mock.patch.object(module, name)
            spies[f"{module.__name__}.{name}"] = patcher.start()
            self.addCleanup(patcher.stop)
        txn_patcher = mock.patch.object(db, "transaction")
        txn = txn_patcher.start()
        self.addCleanup(txn_patcher.stop)

        fin_before = self._dump_state()
        target = 7910001

        # Full ADD flow.
        self._press("ctl:admins:add")
        self._send_text(str(target))
        u1 = self._press(f"ctl:admins:confirm:add:{target}")
        self.assertEqual(_answered(u1.callback_query), TOAST_ADMIN_ADDED)

        # Full REMOVE flow on the same target.
        self._press(f"ctl:admins:remove:{target}")
        u2 = self._press(f"ctl:admins:confirm:remove:{target}")
        self.assertEqual(_answered(u2.callback_query), TOAST_ADMIN_REMOVED)

        txn.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(fin_before, self._dump_state())

    def test_40_no_secrets_in_output_or_logs(self) -> None:
        """40. Rendered cards and log lines carry only safe ids and
        fixed text — never tokens, environment values, paths or raw
        message content."""
        target = 7920001
        self._seed_admin(target, added_by=ADMIN_ID)
        texts = []
        for data in (
            "ctl:admins",
            f"ctl:admins:v:{target}",
            f"ctl:admins:v:{ADMIN_ID}",
            "ctl:admins:add",
            f"ctl:admins:remove:{target}",
            f"ctl:admins:cancel:{target}",
        ):
            _u, text, _m = self._view(data)
            texts.append(text)
        # Re-arm (the cancel above dropped any pending state), then
        # capture the staged confirmation card and a rejection card.
        self._press("ctl:admins:add")
        texts.append(_reply(self._send_text(str(target))))
        texts.append(_reply(self._send_text("not-a-number")))

        with self.assertLogs("admin_control", level="INFO") as cm:
            self._press("ctl:admins")
            self._press("ctl:admins:add")
            self._send_text(str(target))
            self._press(f"ctl:admins:confirm:add:{target}")
            self._press(f"ctl:admins:remove:{target}")
            self._press(f"ctl:admins:confirm:remove:{target}")

        for text in texts:
            self._assert_no_secrets(self, text)
        for line in cm.output:
            self.assertRegex(line, r"admin=\d+")
            lowered = line.lower()
            for needle in ("secret", "password", "token", "initdata"):
                self.assertNotIn(needle, lowered, line)
            self.assertIsNone(_TOKEN_RE.search(line), line)


# ══════════════════════════════════════════════════════════════════
# H. TEXT INPUT SECURITY (extra)
# ══════════════════════════════════════════════════════════════════


class TestAdminsTextInput(AdminsTestBase):

    def test_41_silent_without_pending_state(self) -> None:
        """Ordinary text never triggers the catch-all — no reply, no
        read, no interpretation as a command."""
        with self._db_spies() as (l, g, a, r):
            update = self._send_text("/addtask 1 | 2 | 3 | 4")
        for spy in (l, g, a, r):
            spy.assert_not_called()
        update.message.reply_text.assert_not_called()

    def test_42_non_admin_with_pending_is_silent_and_never_reads(
        self,
    ) -> None:
        """A non-admin holding pending state (armed directly) is
        refused BEFORE validation or any read — silent, no reply."""
        task = 7950001
        self._seed_admin(task)
        admin_control._PENDING_ADMIN_ADD[STRANGER] = {
            "admin": STRANGER,
        }
        with mock.patch.object(
            db, "get_user", wraps=db.get_user
        ) as user_spy, mock.patch.object(
            admin_control, "_parse_admin_id"
        ) as parser:
            update = self._send_text("123456", actor_id=STRANGER)
        parser.assert_not_called()
        user_spy.assert_not_called()
        update.message.reply_text.assert_not_called()
        # Pending untouched — no staged value was armed.
        self.assertNotIn(
            "value", admin_control._PENDING_ADMIN_ADD[STRANGER]
        )

    def test_43_group_chat_with_pending_is_silent(self) -> None:
        """Isolation runs first: even with pending state, a group
        message produces zero replies."""
        admin_control._PENDING_ADMIN_ADD[ADMIN_ID] = {"admin": ADMIN_ID}
        update = self._send_text("123456", chat_type="supergroup")
        update.message.reply_text.assert_not_called()

    def test_44_untrusted_identity_and_non_message_stay_silent(
        self,
    ) -> None:
        """Untrusted/missing identities and non-message updates die
        silently."""
        admin_control._PENDING_ADMIN_ADD[ADMIN_ID] = {"admin": ADMIN_ID}
        update = _update(ADMIN_ID, "123456")
        update.effective_user.id = None
        _run(admin_control.admin_add_text_input(update, mock.MagicMock()))
        update.message.reply_text.assert_not_called()

        update2 = _update(ADMIN_ID, "123456")
        update2.message = None
        _run(admin_control.admin_add_text_input(update2, mock.MagicMock()))

    def test_45_staging_is_scoped_to_one_chat_and_writes_nothing(
        self,
    ) -> None:
        """The staged value lives only in THIS chat's pending state;
        another chat stays untouched and no row is written."""
        self._press("ctl:admins:add")
        before = self._dump_admins()
        # Another private chat (also an admin) without pending →
        # completely silent.
        other = self._send_text("7960001", actor_id=BOOTSTRAP_ID)
        other.message.reply_text.assert_not_called()
        self.assertNotIn(
            BOOTSTRAP_ID, admin_control._PENDING_ADMIN_ADD
        )
        # This chat staged fine — still zero writes.
        reply = self._send_text("7960002")
        self.assertIn("⚠️ تأكيد إضافة مشرف", _reply(reply))
        self.assertEqual(before, self._dump_admins())


# ══════════════════════════════════════════════════════════════════
# I. REGISTRATION (extra) — bot.py untouched
# ══════════════════════════════════════════════════════════════════


class TestAdminsRegistration(unittest.TestCase):

    def test_46_no_static_registration_and_single_ctl_entry(
        self,
    ) -> None:
        """46. One ^ctl: CallbackQueryHandler (group 5), one /control
        CommandHandler (group 0), and NO static registration of the
        add-admin catch-all — it attaches lazily from admin_control."""
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
                admin_control.admin_add_text_input,
                "the add-admin catch-all must attach lazily, not here",
            )

        import bot as bot_mod

        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertNotIn("admin_add_text_input", source)
        self.assertEqual(source.count('pattern=r"^ctl:"'), 1)


class TestAdminsLazyRegistration(AdminsTestBase):

    def test_47_text_handler_attached_once_on_first_press(
        self,
    ) -> None:
        """The first ctl:admins press attaches MessageHandler(group=6)
        ONCE (bot_data marker); later presses never double-register,
        and the MT-ADMIN-36 task catch-all keeps its own group."""
        app = _FakeApplication()
        ctx = SimpleNamespace(application=app)
        for _ in range(2):
            update = _callback(ADMIN_ID, "ctl:admins")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
        self.assertEqual(len(app.added), 1)
        handler, group = app.added[0]
        self.assertEqual(group, 6)
        self.assertIs(
            handler.callback, admin_control.admin_add_text_input
        )
        self.assertTrue(
            app.bot_data[admin_control._ADMINS_TEXT_HANDLER_MARK]
        )

        # Both catch-alls coexist — one per group.
        app2 = _FakeApplication()
        ctx2 = SimpleNamespace(application=app2)
        for data in ("ctl:tasks", "ctl:admins"):
            update = _callback(ADMIN_ID, data)
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx2))
        self.assertEqual(
            [group for _h, group in app2.added], [3, 6]
        )

    def test_48_context_without_live_application_is_noop(self) -> None:
        """Unit-test shims (no Application / MagicMock bot_data)
        degrade to a no-op — the view still renders."""
        for ctx in (SimpleNamespace(), mock.MagicMock()):
            update = _callback(ADMIN_ID, "ctl:admins")
            update.callback_query.message.reply_text = mock.AsyncMock()
            _run(admin_control.control_callback(update, ctx))
            update.callback_query.edit_message_text.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════
# J. NAVIGATION (extra) — Control Center intact
# ══════════════════════════════════════════════════════════════════


class TestAdminsNavigation(AdminsTestBase):

    def test_49_registry_and_dashboard_entry_intact(self) -> None:
        """The frozen registry keeps the same key set; admins renders
        in place (command=None) and the dashboard still offers every
        module with the exact payload set."""
        expected_keys = {
            "users", "tasks", "reviews", "withdrawals", "deposits",
            "paymethods", "rate", "rewards", "broadcast", "settings",
            "admins", "logs", "health",
        }
        self.assertEqual(set(admin_control.MODULES_BY_KEY), expected_keys)
        self.assertIsNone(admin_control.MODULES_BY_KEY["admins"].command)
        self.assertIsNone(admin_control.MODULES_BY_KEY["users"].command)
        self.assertIsNone(admin_control.MODULES_BY_KEY["tasks"].command)
        self.assertNotIn("admins", admin_control._NAVIGATORS)

        payloads = self._payloads(build_dashboard_keyboard())
        self.assertEqual(
            sorted(payloads),
            sorted(
                [f"ctl:{key}" for key in expected_keys]
                + ["ctl:refresh"]
            ),
        )
        update = self._cmd()
        self.assertIn(HEADER, _reply(update))

    def test_50_admins_press_renders_not_unavailable(self) -> None:
        """50. The formerly-reserved admins slot is functional: the
        press EDITs the panel instead of the unavailable notice."""
        update = self._press("ctl:admins")
        self.assertIn(ADMINS_PANEL_HEADER, _edited(update.callback_query))
        self.assertNotEqual(
            _answered(update.callback_query), MSG_MODULE_UNAVAILABLE
        )

    def test_51_reserved_modules_still_unavailable(self) -> None:
        """51. The other reserved slots keep their safe notice — no
        reads, no render, no claim of functionality."""
        with mock.patch.object(admin_control, "collect_snapshot") as snap:
            for key in (
                # "broadcast" moved to test_admin_broadcast.py
                # (MT-ADMIN-38 renders it in place).
                "rewards", "settings", "logs", "health",
            ):
                update = self._press(f"ctl:{key}")
                self.assertEqual(
                    _answered(update.callback_query),
                    MSG_MODULE_UNAVAILABLE,
                    key,
                )
                update.callback_query.edit_message_text.assert_not_called()
        snap.assert_not_called()

    def test_52_back_refresh_and_sibling_modules_route_correctly(
        self,
    ) -> None:
        """52. admins:back → panel, ctl:refresh → dashboard, and the
        users/tasks modules still render in place."""
        _u, text, _m = self._view(f"ctl:admins:v:{ADMIN_ID}")
        self.assertIn(ADMIN_DETAIL_HEADER, text)
        _u2, text2, _m2 = self._view("ctl:admins:back")
        self.assertIn(ADMINS_PANEL_HEADER, text2)

        update = self._press("ctl:refresh")
        self.assertIn(HEADER, _edited(update.callback_query))
        update.callback_query.answer.assert_awaited()

        _u3, text3, _m3 = self._view("ctl:users")
        self.assertIn("إدارة المستخدمين", text3)
        _u4, text4, _m4 = self._view("ctl:tasks")
        self.assertIn(TASKS_PANEL_HEADER, text4)


# ══════════════════════════════════════════════════════════════════
# K. GRAMMAR (extra) — parser-level guarantees
# ══════════════════════════════════════════════════════════════════


class TestAdminsCallbackGrammar(unittest.TestCase):

    def test_canonicalization_and_bounds(self) -> None:
        # Canonical ops pass through unchanged.
        self.assertEqual(parse_callback("ctl:admins"), "admins")
        self.assertEqual(parse_callback("ctl:admins:back"), "admins:back")
        self.assertEqual(parse_callback("ctl:admins:add"), "admins:add")
        self.assertEqual(parse_callback("ctl:refresh"), "refresh")
        # Numeric payloads canonicalize to one spelling.
        self.assertEqual(parse_callback("ctl:admins:p:007"), "admins:p:7")
        self.assertEqual(parse_callback("ctl:admins:p:0"), "admins:p:0")
        self.assertEqual(
            parse_callback("ctl:admins:v:00501"), "admins:v:501"
        )
        self.assertEqual(
            parse_callback("ctl:admins:remove:009"), "admins:remove:9"
        )
        self.assertEqual(
            parse_callback("ctl:admins:cancel:009"), "admins:cancel:9"
        )
        self.assertEqual(
            parse_callback("ctl:admins:confirm:add:009"),
            "admins:confirm:add:9",
        )
        self.assertEqual(
            parse_callback("ctl:admins:confirm:remove:009"),
            "admins:confirm:remove:9",
        )
        # Out-of-grammar payloads fail safely.
        for bad in (
            "ctl:admins:",
            "ctl:admins:p:",
            "ctl:admins:p:-1",
            "ctl:admins:p:1.5",
            "ctl:admins:p:" + "9" * 10,      # > 9 digits
            "ctl:admins:v:" + "9" * 16,      # > 15 digits
            "ctl:admins:v:abc",
            "ctl:admins:add:1",
            "ctl:admins:confirm:delete:1",
            "ctl:admins:confirm:add:",
            "ctl:admins;v:1",
            "ctl:adminsp:1",
            "ctl:ADMINS",
            None,
            7,
            True,
        ):
            self.assertIsNone(parse_callback(bad), bad)

    def test_foreign_namespaces_untouched(self) -> None:
        """wd:/dp:/pm:/mr:/atw:/mproof:/sup: stay outside this
        grammar — the admins parser never claims them, and the
        siblings (users/tasks) still parse."""
        for data in (
            "wd:x", "dp:x", "pm:x", "mr:view:1", "mr:vp:1",
            "atw:x", "mproof:x", "sup:x",
        ):
            self.assertIsNone(parse_callback(data), data)
        self.assertEqual(parse_callback("ctl:users:v:501"), "users:v:501")
        self.assertEqual(parse_callback("ctl:tasks:v:5"), "tasks:v:5")


if __name__ == "__main__":
    unittest.main()
