"""
Focused tests — Admin user management (MT-ADMIN-35)
====================================================

The ``users`` module of the Admin Control Center: a read-only,
in-place user-management surface built strictly on the authoritative
store reads (``db.count_users`` / ``db.list_users`` / ``db.get_user``).

Coverage required by MT-ADMIN-35 §17:

A. AUTHORIZATION (1-5)
   1  non-admin ``/control`` cannot read users (no data is read)
   2  non-admin ``ctl:users*`` refused BEFORE any user read
   3  group ``/control`` is silent
   4  group ``ctl:users*`` is silent and reads nothing
   5  callback authorization is re-checked at every entry

B. DASHBOARD (6-8)
   6  authoritative user count displayed — same source as the module
   7  zero users handled safely (count + empty panel)
   8  existing dashboard modules remain visible

C. LIST (9-14)
   9  users are paginated (PAGE_SIZE rows per page)
  10  page size is bounded (module PAGE_SIZE + store hard limit)
  11  deterministic ordering (user_id DESC, identical across calls)
  12  empty page handled safely (clamped, never crashes)
  13  first-page navigation correct (next only, + control-center back)
  14  last-page navigation clamped (prev only, correct rows)

D. DETAIL (15-18)
  15  valid user detail with safe fields
  16  unknown user handled safely
  17  detail exposes safe administrative fields ONLY
  18  recursive output excludes keys, destinations, proofs, env values

E. CALLBACK SAFETY (19-23)
  19  malformed users payloads fail safely (invalid, no reads)
  20  oversized page: beyond grammar → rejected; within → clamped
  21  oversized user id: rejected beyond grammar, safe when unknown
  22  stale callbacks (edit/answer raise) degrade to safe no-ops
  23  unsupported operations fail safely

F. MUTATION SAFETY (24-30)
  24  no transaction on the users dashboard
  25  no transaction on the user list
  26  no transaction on the user detail
  27-30 wallet/ledger, withdrawal/deposit, payment-method and rate
      stores untouched on every users path (+ full row dumps)

G. REGRESSION (31-35)
  31  ``/control`` registered exactly once
  32  ``^ctl:`` registered exactly once
  33  foreign callback namespaces untouched
  34  existing control-center modules still route correctly
  35  ``ctl:refresh`` still works (now with the user count)

Temp databases only; no production destinations or balances are used.

Run:
    .venv/bin/python -m pytest test_admin_users.py -v
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from unittest import mock

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import db

from admin_control import (
    HEADER,
    MSG_ADMIN_ONLY,
    MSG_ERROR,
    MSG_INVALID,
    MSG_USER_NOT_FOUND,
    USER_DETAIL_HEADER,
    USERS_HEADER,
    USERS_PANEL_HEADER,
    build_dashboard_text,
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
    PROOF_KEY,
    ControlTestBase,
)

from test_withdrawal_service import ADMIN_ID, PM_DESTINATION, USER_DEST

STRANGER = 999_999

ENV_SECRET = "USERS-ENV-SENTINEL-42bb"

# Every sensitive value that must never reach a users-surface output.
FORBIDDEN_KEYS = (
    "pm_destination",
    "destination",
    "init_data",
    "initdata",
    "token",
    "secret",
    "password",
    "api_key",
    "rpc_url",
    "storage_key",
)


def _walk(node):
    """Yield every string (and mapping key) inside a nested output."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk(value)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for item in node:
            yield from _walk(item)


def _capture_handlers():
    """Run ``bot.main()`` against a mock app — collect registrations."""
    import bot as bot_mod

    captured: list = []
    app = mock.MagicMock()
    app.add_handler = lambda handler, group=None: captured.append(
        (handler, group)
    )
    builder = mock.MagicMock()
    builder.token.return_value.build.return_value = app
    with mock.patch.dict(
        os.environ, {"TELEGRAM_BOT_TOKEN": "12345:TESTTOKEN"}
    ), mock.patch.object(
        bot_mod, "ApplicationBuilder", return_value=builder
    ), mock.patch.object(
        bot_mod, "run_single_entry"
    ), mock.patch.object(bot_mod, "db"):
        bot_mod.main()
    return captured, bot_mod


class UsersTestBase(ControlTestBase):
    """MT-ADMIN-35 fixture: the control-center drivers plus user
    seeding and users-view helpers."""

    # ── seeds ────────────────────────────────────────────────────

    def _seed_users(self, *ids: int) -> None:
        for uid in ids:
            self.assertTrue(db.register_user(uid, f"u{uid}", f"U{uid}"))

    # ── users-view drivers ───────────────────────────────────────

    def _view(self, data: str = "ctl:users", **kwargs):
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


# ══════════════════════════════════════════════════════════════════
# A. AUTHORIZATION (1-5)
# ══════════════════════════════════════════════════════════════════


class TestUsersAuthorization(UsersTestBase):

    def test_01_non_admin_control_reads_no_users(self) -> None:
        """1. Non-admins get the standard refusal BEFORE any read."""
        with mock.patch.object(db, "count_users") as count, \
                mock.patch.object(db, "list_users") as listing:
            update = self._cmd(actor_id=STRANGER)
        count.assert_not_called()
        listing.assert_not_called()
        self.assertEqual(_reply(update), MSG_ADMIN_ONLY)

    def test_02_non_admin_users_press_refused_before_any_read(
        self,
    ) -> None:
        """2. Every ``ctl:users*`` entry re-checks admin BEFORE any
        user data is read — no render, no lookup, no side effect."""
        for data in (
            "ctl:users",
            "ctl:users:p:1",
            "ctl:users:v:501",
            "ctl:users:back",
        ):
            with mock.patch.object(db, "count_users") as count, \
                    mock.patch.object(db, "list_users") as listing, \
                    mock.patch.object(db, "get_user") as get_user:
                update = self._press(data, actor_id=STRANGER)
            count.assert_not_called()
            listing.assert_not_called()
            get_user.assert_not_called()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, data
            )
            update.callback_query.edit_message_text.assert_not_called()

    def test_03_group_control_is_silent(self) -> None:
        """3. Group ``/control`` produces ZERO replies."""
        update = self._cmd(chat_type="supergroup")
        update.message.reply_text.assert_not_called()

    def test_04_group_users_press_is_silent_and_reads_nothing(
        self,
    ) -> None:
        """4. Group presses reveal nothing and read nothing."""
        for data in (
            "ctl:users",
            "ctl:users:p:1",
            "ctl:users:v:501",
            "ctl:users:back",
        ):
            with mock.patch.object(db, "count_users") as count, \
                    mock.patch.object(db, "get_user") as get_user:
                update = self._press(data, chat_type="supergroup")
            count.assert_not_called()
            get_user.assert_not_called()
            self.assertIsNone(_answered(update.callback_query), data)
            update.callback_query.edit_message_text.assert_not_called()

    def test_05_callback_authorization_rechecked_server_side(
        self,
    ) -> None:
        """5. Even a press that LOOKS like it came from the Control
        Center is re-gated through config.is_admin — flipping the
        gate makes every users entry refuse with zero reads."""
        with mock.patch.object(
            admin_control, "is_admin", return_value=False
        ):
            for data in (
                "ctl:users",
                "ctl:users:p:0",
                "ctl:users:v:1",
                "ctl:users:back",
            ):
                with mock.patch.object(db, "count_users") as count, \
                        mock.patch.object(db, "get_user") as get_user:
                    update = self._press(data)
                count.assert_not_called()
                get_user.assert_not_called()
                self.assertEqual(
                    _answered(update.callback_query), MSG_ADMIN_ONLY, data
                )
                update.callback_query.edit_message_text.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# B. DASHBOARD (6-8)
# ══════════════════════════════════════════════════════════════════


class TestUsersDashboard(UsersTestBase):

    def test_06_dashboard_shows_authoritative_user_count(self) -> None:
        """6. ``/control`` shows ``db.count_users()`` — the ONE
        authoritative source, no cached or duplicated count."""
        self._seed_users(501, 502, 503)
        text = _reply(self._cmd())
        self.assertIn(f"{USERS_HEADER}: 3", text)

        with mock.patch.object(db, "count_users", return_value=4242) as src:
            text2 = _reply(self._cmd())
        self.assertTrue(src.called)
        self.assertIn(f"{USERS_HEADER}: 4242", text2)

    def test_06b_users_module_reads_the_same_source(self) -> None:
        """6b. The users panel total comes from the SAME read — the
        module never invents its own count."""
        with mock.patch.object(db, "count_users", return_value=4242) as src:
            _update_, text, markup = self._view("ctl:users")
        self.assertTrue(src.called)
        self.assertIn(f"👥 إجمالي المستخدمين: 4242", text)
        self.assertIn(USERS_PANEL_HEADER, text)
        # Non-authoritative classifications stay honestly unavailable.
        self.assertIn("🟢 النشطون: غير متاح", text)
        self.assertIn("📅 الجدد: غير متاح", text)
        # The message is a real rendered view with a way back.
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertIn("ctl:refresh", self._buttons(markup))

    def test_07_zero_users_handled(self) -> None:
        """7. Empty database: count 0 and a safe empty list state."""
        text = _reply(self._cmd())
        self.assertIn(f"{USERS_HEADER}: 0", text)

        _update_, panel, markup = self._view("ctl:users")
        self.assertIn("👥 إجمالي المستخدمين: 0", panel)
        self.assertIn("لا يوجد مستخدمون بعد.", panel)
        self.assertIn("صفحة 1/1", panel)
        self.assertNotIn(
            "Traceback", panel,
        )
        self.assertIsInstance(markup, InlineKeyboardMarkup)

    def test_07b_dashboard_degrades_without_source(self) -> None:
        """7b. A missing snapshot degrades the metric to غير متاح —
        never a fabricated number."""
        self.assertIn(
            f"{USERS_HEADER}: {admin_control.NA}", build_dashboard_text({})
        )

    def test_08_existing_dashboard_modules_remain_visible(self) -> None:
        """8. Implementing users changes none of the other cards or
        registry buttons."""
        from admin_control import (
            DEPOSITS_HEADER,
            PAYMETHODS_HEADER,
            RATE_HEADER,
            TASKS_HEADER,
            WITHDRAWALS_HEADER,
        )

        self._seed_users(501)
        update = self._cmd()
        text = _reply(update)
        for header in (
            HEADER,
            USERS_HEADER,
            TASKS_HEADER,
            WITHDRAWALS_HEADER,
            DEPOSITS_HEADER,
            PAYMETHODS_HEADER,
            RATE_HEADER,
        ):
            self.assertIn(header, text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        payloads = self._buttons(markup)
        for module in admin_control.MODULES:
            self.assertIn(f"ctl:{module.key}", payloads)
        self.assertIn("ctl:refresh", payloads)


# ══════════════════════════════════════════════════════════════════
# C. LIST (9-14)
# ══════════════════════════════════════════════════════════════════


class TestUsersList(UsersTestBase):

    def test_09_users_are_paginated(self) -> None:
        """9. 7 users → first page holds PAGE_SIZE rows, the second
        page the remainder."""
        self._seed_users(*range(501, 508))

        _u, text, markup = self._view("ctl:users")
        user_labels = [l for l in self._labels(markup) if l.startswith("👤")]
        self.assertEqual(
            len(user_labels), admin_control.USERS_PAGE_SIZE
        )
        self.assertIn("صفحة 1/2", text)

        _u2, text2, markup2 = self._view("ctl:users:p:1")
        user_labels2 = [
            l for l in self._labels(markup2) if l.startswith("👤")
        ]
        self.assertEqual(
            len(user_labels2), 7 - admin_control.USERS_PAGE_SIZE
        )
        self.assertIn("صفحة 2/2", text2)

    def test_10_page_size_is_bounded(self) -> None:
        """10. A small fixed module page AND a store-level hard limit
        — no caller can ever trigger an unbounded read."""
        self.assertEqual(admin_control.USERS_PAGE_SIZE, 5)
        self.assertLessEqual(admin_control.USERS_PAGE_SIZE, 10)

        # Store bound: even a huge limit is clamped to the max.
        total = db.MAX_USER_LIST_LIMIT + 5
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                "INSERT INTO users (user_id, username, first_name) "
                "VALUES (?, ?, ?)",
                [(90_000 + i, f"x{i}", f"X{i}") for i in range(total)],
            )
            conn.commit()
        finally:
            conn.close()
        rows = db.list_users(10**9)
        self.assertEqual(len(rows), db.MAX_USER_LIST_LIMIT)

        # Non-positive / wrongly-typed bounds resolve safely.
        self.assertEqual(db.list_users(0), [])
        self.assertEqual(db.list_users(-3), [])
        self.assertRaises(TypeError, db.list_users, "5")
        self.assertRaises(TypeError, db.list_users, True)
        self.assertRaises(TypeError, db.list_users, 5, offset=True)

    def test_11_deterministic_ordering(self) -> None:
        """11. user_id DESC (newest first) with a unique tiebreaker —
        repeated reads are byte-identical."""
        self._seed_users(101, 105, 103, 102, 104)
        first = db.list_users(5)
        second = db.list_users(5)
        self.assertEqual(first, second)
        self.assertEqual(
            [row["user_id"] for row in first],
            [105, 104, 103, 102, 101],
        )

        # The rendered view is equally stable across presses.
        _u1, _t1, m1 = self._view("ctl:users")
        _u2, _t2, m2 = self._view("ctl:users")
        self.assertEqual(self._buttons(m1), self._buttons(m2))

    def test_12_empty_page_clamped_safely(self) -> None:
        """12. A page beyond an empty list clamps to page 1/1 with a
        safe empty state — no crash, no error text."""
        _u, text, markup = self._view("ctl:users:p:5")
        self.assertIn("لا يوجد مستخدمون بعد.", text)
        self.assertIn("صفحة 1/1", text)
        self.assertNotIn("Traceback", text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)

    def test_13_first_page_navigation_correct(self) -> None:
        """13. First page: NEXT only (no prev), plus the canonical
        control-center back action."""
        self._seed_users(*range(501, 508))
        _u, _text, markup = self._view("ctl:users")
        payloads = self._buttons(markup)
        self.assertIn("ctl:users:p:1", payloads)     # next page
        self.assertNotIn("ctl:users:p:0", payloads)  # no prev on page 0
        self.assertIn("ctl:refresh", payloads)       # ← control center

    def test_14_last_page_navigation_clamped(self) -> None:
        """14. An oversized page index clamps to the REAL last page —
        prev only, correct rows."""
        self._seed_users(*range(501, 508))
        _u, text, markup = self._view("ctl:users:p:999")
        self.assertIn("صفحة 2/2", text)
        payloads = self._buttons(markup)
        self.assertIn("ctl:users:p:0", payloads)     # prev → first
        self.assertNotIn("ctl:users:p:2", payloads)  # no next on last
        user_labels = [l for l in self._labels(markup) if l.startswith("👤")]
        self.assertEqual(len(user_labels), 2)  # 502 + 501 remain
        joined = "\n".join(user_labels)
        self.assertIn("u502", joined)
        self.assertIn("u501", joined)


# ══════════════════════════════════════════════════════════════════
# D. DETAIL (15-18)
# ══════════════════════════════════════════════════════════════════


class TestUsersDetail(UsersTestBase):

    def test_15_valid_user_detail(self) -> None:
        """15. A real user renders the safe administrative profile
        from the authoritative ``db.get_user`` read."""
        self._seed_users(501)
        _u, text, markup = self._view("ctl:users:v:501")
        self.assertIn(USER_DETAIL_HEADER, text)
        self.assertIn("🆔 Telegram ID: 501", text)
        self.assertIn("👤 Username: @u501", text)
        self.assertIn("📛 الاسم: U501", text)
        self.assertRegex(text, r"📅 التسجيل: \d{4}-\d{2}-\d{2}")
        # No authoritative last-seen column → honestly unavailable.
        self.assertIn("🕒 آخر نشاط: غير متاح", text)
        # Only navigation: back to the user list.
        self.assertEqual(self._buttons(markup), ["ctl:users:back"])

    def test_15b_user_without_username(self) -> None:
        """15b. Missing optional fields degrade per-field, not the
        whole view."""
        self.assertTrue(db.register_user(601, None, "NoNick"))
        _u, text, _m = self._view("ctl:users:v:601")
        self.assertIn("🆔 Telegram ID: 601", text)
        self.assertIn("👤 Username: غير متاح", text)
        self.assertIn("📛 الاسم: NoNick", text)

    def test_16_unknown_user_handled_safely(self) -> None:
        """16. A stale/unknown id answers a fixed safe notice — never
        a crash, traceback or database error."""
        _u, text, markup = self._view("ctl:users:v:424242")
        self.assertEqual(text, MSG_USER_NOT_FOUND)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("sqlite", text.lower())
        # Back navigation still offered.
        self.assertEqual(self._buttons(markup), ["ctl:users:back"])

    def test_17_detail_safe_fields_only(self) -> None:
        """17. Every line of the detail is one of the documented
        administrative identity labels — nothing else."""
        self._seed_users(501)
        _u, text, _m = self._view("ctl:users:v:501")
        allowed_prefixes = ("🆔", "👤", "📛", "📅", "🕒")
        for line in text.splitlines():
            if not line or line == USER_DETAIL_HEADER:
                continue
            self.assertTrue(
                line.startswith(allowed_prefixes),
                f"unexpected detail line: {line!r}",
            )

    def test_18_users_surface_excludes_sensitive_data(self) -> None:
        """18. Recursive-output safety: across dashboard, list, detail
        and back views, no storage key, platform/user destination,
        environment value or sensitive key can appear."""
        # Seed rows that genuinely carry sensitive values.
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        self._seed_users(503, 504)

        outputs = []
        with mock.patch.dict(
            os.environ, {"ADMIN_USERS_TEST_SECRET": ENV_SECRET}
        ):
            outputs.append(_reply(self._cmd()))
            for data in (
                "ctl:users",
                "ctl:users:p:0",
                "ctl:users:v:503",
                "ctl:users:back",
            ):
                _u, text, markup = self._view(data)
                outputs.append(text)
                for row in markup.inline_keyboard:
                    for button in row:
                        outputs.append(button.text)
                        outputs.append(button.callback_data)

        flat = [s for node in outputs for s in _walk(node)]
        lowered = [s.lower() for s in flat]
        for key in FORBIDDEN_KEYS:
            for s in lowered:
                self.assertNotIn(key, s, f"sensitive key leaked: {key}")
        for value in (PROOF_KEY, PM_DESTINATION, USER_DEST, ENV_SECRET):
            for s in lowered:
                self.assertNotIn(value.lower(), s, "secret value leaked")


# ══════════════════════════════════════════════════════════════════
# E. CALLBACK SAFETY (19-23)
# ══════════════════════════════════════════════════════════════════


class TestUsersCallbackSafety(UsersTestBase):

    def _assert_invalid(self, data) -> None:
        with mock.patch.object(db, "count_users") as count, \
                mock.patch.object(db, "list_users") as listing, \
                mock.patch.object(db, "get_user") as get_user:
            update = self._press(data)
        self.assertEqual(_answered(update.callback_query), MSG_INVALID, data)
        count.assert_not_called()
        listing.assert_not_called()
        get_user.assert_not_called()
        update.callback_query.edit_message_text.assert_not_called()

    def test_19_malformed_users_payloads_fail_safely(self) -> None:
        """19. Malformed payloads are rejected by the grammar — no
        crash, no reads, no database error surfaced."""
        for data in (
            "ctl:users:",
            "ctl:users:x",
            "ctl:users:p:",
            "ctl:users:p:abc",
            "ctl:users:p:-1",
            "ctl:users:p:+1",
            "ctl:users:v:",
            "ctl:users:v:abc",
            "ctl:users:v:-5",
            "ctl:users:p:1:",
            "ctl:users:v:5a",
            "ctl:users:p:1 ",
            None,
        ):
            self._assert_invalid(data)

    def test_20_oversized_page(self) -> None:
        """20. Beyond the bounded grammar → rejected before any read;
        within the grammar → clamped to the real range, no crash."""
        self._assert_invalid("ctl:users:p:99999999999")  # 11 digits
        self._assert_invalid("ctl:users:p:" + "9" * 40)

        _u, text, _m = self._view("ctl:users:p:999999999")  # 9 digits
        self.assertIn("صفحة 1/1", text)  # 0 users → clamped, safe

    def test_21_oversized_user_id(self) -> None:
        """21. An id beyond the 15-digit grammar is rejected before
        any read; an in-grammar unknown id answers the safe notice."""
        self._assert_invalid("ctl:users:v:999999999999999999")  # 18 d
        self._assert_invalid("ctl:users:v:" + "9" * 40)

        _u, text, _m = self._view("ctl:users:v:999999999999999")  # 15 d
        self.assertEqual(text, MSG_USER_NOT_FOUND)

    def test_22_stale_callbacks_fail_safely(self) -> None:
        """22. Old messages/queries (edit and answer both raising)
        degrade to safe no-ops — the handler never throws."""
        update = _callback(ADMIN_ID, "ctl:users")
        update.callback_query.message.reply_text = mock.AsyncMock()
        update.callback_query.edit_message_text = mock.AsyncMock(
            side_effect=Exception("message too old")
        )
        update.callback_query.answer = mock.AsyncMock(
            side_effect=Exception("query too old")
        )
        _run(admin_control.control_callback(update, mock.MagicMock()))
        update.callback_query.edit_message_text.assert_awaited_once()

        update2 = _callback(ADMIN_ID, "ctl:users:v:501")
        update2.callback_query.message.reply_text = mock.AsyncMock()
        update2.callback_query.edit_message_text = mock.AsyncMock(
            side_effect=Exception("message too old")
        )
        _run(admin_control.control_callback(update2, mock.MagicMock()))
        update2.callback_query.edit_message_text.assert_awaited_once()

    def test_23_unsupported_operation(self) -> None:
        """23. Unknown ops — inside and outside the users family —
        fail safely with the standard invalid answer."""
        for data in (
            "ctl:USERS",
            "ctl:users ",
            "ctl:users:ops:1",
            "ctl:user",
            "ctl:users:v",
            "ctl:users:p",
        ):
            self._assert_invalid(data)

    def test_23b_view_failure_degrades_to_safe_error(self) -> None:
        """23b. A failing authoritative read answers the fixed safe
        error — never a traceback or the internal message."""
        with mock.patch.object(
            db, "count_users", side_effect=RuntimeError("boom-users")
        ):
            update = self._press("ctl:users")
        answer = _answered(update.callback_query)
        self.assertEqual(answer, MSG_ERROR)
        self.assertNotIn("boom-users", answer)
        self.assertNotIn("Traceback", answer)
        update.callback_query.edit_message_text.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# F. MUTATION SAFETY (24-30)
# ══════════════════════════════════════════════════════════════════


class TestUsersMutationSafety(UsersTestBase):

    def test_24_dashboard_opens_no_transaction(self) -> None:
        """24. ``ctl:users`` never enters db.transaction()."""
        self._seed_users(501)
        with mock.patch.object(db, "transaction") as txn:
            self._press("ctl:users")
        txn.assert_not_called()

    def test_25_user_list_opens_no_transaction(self) -> None:
        """25. Paging (``ctl:users:p:*``) never enters a
        transaction."""
        self._seed_users(501, 502, 503, 504, 505, 506)
        with mock.patch.object(db, "transaction") as txn:
            self._press("ctl:users:p:1")
        txn.assert_not_called()

    def test_26_user_detail_opens_no_transaction(self) -> None:
        """26. ``ctl:users:v:*`` and ``ctl:users:back`` never enter a
        transaction."""
        self._seed_users(501)
        with mock.patch.object(db, "transaction") as txn:
            self._press("ctl:users:v:501")
            self._press("ctl:users:back")
        txn.assert_not_called()

    def test_27_30_financial_stores_untouched_on_every_users_path(
        self,
    ) -> None:
        """27-30. Wallet/ledger, withdrawal, deposit, payment-method
        and rate mutation entry points never run on any users path —
        proven by spies AND byte-identical row dumps of every
        financial table."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        self._seed_users(503, 504)

        spies = {}
        for module, name in MUTATION_SPY_TARGETS:
            patcher = mock.patch.object(module, name)
            spies[f"{module.__name__}.{name}"] = patcher.start()
            self.addCleanup(patcher.stop)
        txn_patcher = mock.patch.object(db, "transaction")
        txn = txn_patcher.start()
        self.addCleanup(txn_patcher.stop)

        before = self._dump_state()
        self._cmd()
        for data in (
            "ctl:users",
            "ctl:users:p:0",
            "ctl:users:p:1",
            "ctl:users:v:501",
            "ctl:users:v:999999",
            "ctl:users:back",
            "ctl:refresh",
        ):
            self._press(data)

        txn.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())
        # And the dumps really cover the financial tables.
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
# G. REGRESSION (31-35)
# ══════════════════════════════════════════════════════════════════


class TestUsersRegistration(unittest.TestCase):

    """31-33. Registration integrity after the users module landed."""

    def test_31_control_command_registered_exactly_once(self) -> None:
        """31. One CommandHandler("control") in group 0."""
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

    def test_32_ctl_callback_registered_exactly_once(self) -> None:
        """32. One CallbackQueryHandler on ^ctl: in group 5."""
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

    def test_33_existing_callback_namespaces_untouched(self) -> None:
        """33. Foreign callback families stay registered exactly once;
        this module builds no foreign payload."""
        import bot as bot_mod

        source = open(bot_mod.__file__, encoding="utf-8").read()
        for pattern in (
            'pattern=r"^wd:"',
            'pattern=r"^dp:"',
            'pattern=r"^pm:"',
            'pattern=r"^mr(view|vp):"',
            'pattern=r"^atw:"',
            'pattern=r"^mproof:"',
            'pattern=r"^ctl:"',
        ):
            self.assertEqual(
                source.count(pattern), 1, f"{pattern} must stay unique"
            )

        ctl_source = open(admin_control.__file__, encoding="utf-8").read()
        for foreign in (
            'callback_data="wd',
            'callback_data="dp',
            'callback_data="pm',
            'callback_data="mr',
            'callback_data="atw',
        ):
            self.assertNotIn(foreign, ctl_source)


class TestUsersRegression(UsersTestBase):

    """34-35. Existing control-center behavior stays intact."""

    def test_34_existing_modules_still_route(self) -> None:
        """34. Every pre-existing module still delegates to ITS
        existing command surface."""
        import bot as bot_mod
        import deposit_proof_admin
        import payment_method_admin
        import rate_admin
        import withdrawal_admin
        import admin_review_queue

        target_map = {
            "tasks": (bot_mod, "list_tasks"),
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

    def test_35_refresh_still_works(self) -> None:
        """35. ``ctl:refresh`` still re-renders the dashboard in
        place — now carrying the authoritative user count."""
        self._seed_users(501, 502)
        update = self._press("ctl:refresh")
        text = _edited(update.callback_query)
        self.assertIn(HEADER, text)
        self.assertIn(f"{USERS_HEADER}: 2", text)
        update.callback_query.answer.assert_awaited()


class TestUsersCallbackGrammar(unittest.TestCase):

    """Parser-level guarantees for the users sub-grammar."""

    def test_canonicalization_and_bounds(self) -> None:
        # Canonical registry ops pass through unchanged.
        self.assertEqual(parse_callback("ctl:users"), "users")
        self.assertEqual(parse_callback("ctl:refresh"), "refresh")
        # Numeric payloads canonicalize to one spelling.
        self.assertEqual(parse_callback("ctl:users:p:007"), "users:p:7")
        self.assertEqual(parse_callback("ctl:users:p:0"), "users:p:0")
        self.assertEqual(
            parse_callback("ctl:users:v:00501"), "users:v:501"
        )
        self.assertEqual(parse_callback("ctl:users:back"), "users:back")
        # Out-of-grammar payloads fail safely.
        for bad in (
            "ctl:users:p:",
            "ctl:users:p:-1",
            "ctl:users:p:1.5",
            "ctl:users:p:" + "9" * 10,      # > 9 digits
            "ctl:users:v:" + "9" * 16,      # > 15 digits
            "ctl:users:v:abc",
            "ctl:users:garbage",
            "ctl:users:v:501x",
            "ctl:tasksp:1",
            "ctl:users;p:1",
            None,
            7,
            True,
        ):
            self.assertIsNone(parse_callback(bad), bad)


if __name__ == "__main__":
    unittest.main()
