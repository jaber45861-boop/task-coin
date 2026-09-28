"""
Focused tests — Admin Control Center Foundation (MT-ADMIN-32)
==============================================================

The thin, read-only control layer over the EXISTING admin surfaces:
``/control`` dashboard + ``ctl:<surface>`` navigation callbacks.

Coverage required by MT-ADMIN-32:

A. COMMAND AUTH (1-6)
   1  admin can open the Control Center
   2  non-admin cannot access it (and no data is even read)
   3  group chat -> zero replies (MT-ADMIN-02 isolation convention)
   4  channel chat -> zero replies
   5  untrusted/missing identity and missing message stay silent
   6  render failure -> safe Arabic error, never a traceback

B. METRICS / SOURCES (7-16)
   7  withdrawal count comes from SqliteWithdrawalRepository.list_pending
   8  deposit count comes from deposit_proof_store.list_pending_proofs
   9  payment-method counts come from payment_method_store
  10  rate display uses rate_store.get_current_quote (never platform_settings)
  11  fresh rate renders ``USDT/EGP: 48.5 — صالح``
  12  missing rate -> ``غير متاح``, no crash
  13  stale rate -> ``غير متاح``, no crash
  14  a failing metric degrades to ``غير متاح`` and is logged
  15  task counts: active from db.list_tasks, pending review from the
      existing admin_review_queue read (real pending claim + source spy)
  16  no invented deposit aggregate + no SQL statements in the module

C. NAVIGATION (17-24)
  17  keyboard carries exactly the fixed ``ctl:<surface>`` set
  18  admin press ctl:withdrawals lands on the EXISTING /withdrawals queue
  19  admin press ctl:deposits lands on the EXISTING /deposits queue
  20  admin press ctl:tasks lands on the EXISTING /listtasks surface
      (and ctl:rate on the EXISTING /setrate surface)
  21  delegated handler receives the real identity + private chat
  22  non-admin press refused; target handler never invoked
  23  group press answered with no data; target handler never invoked
  24  malformed / stale / failing presses fail safely

D. SAFETY / SECURITY (25-31)
  25  opening performs NO db.transaction (BEGIN not required)
  26  opening changes no wallet/ledger/withdrawal/deposit/pm/rate/task row
  27  no financial mutation entry point runs on open or on any press
  28  callback payloads carry only fixed ctl identifiers (no ids/secrets)
  29  output excludes payment destinations, proof storage keys, env values
  30  output excludes user identity, wallet balances, destinations
  31  module contains no SQL statements (existing interfaces only)

E. REGISTRATION (32-34)
  32  /control registered exactly once in bot.main() (group 0)
  33  ctl: callback registered exactly once (group 5)
  34  exactly one "control" / "^ctl:" registration literal in bot.py

G. MT-ADMIN-33 — OPERATIONAL EXTENSION
  - attention-first keyboard: decision queues lead; ``ctl:reviews``
    added to the closed payload set (still fixed ids only)
  - rate card shows value + provider + freshness state, all from the
    one ``rate_store.get_current_quote()`` quote
  - no rate read from ``platform_settings`` (structural + runtime)
  - EVERY ctl op re-checks authorization (non-admin AND group for all
    six surfaces — the target handler is never invoked)
  - ``ctl:reviews`` delegates to the EXISTING /reviews queue (real
    empty + real pending-claim renders; shim identity checked)
  - mutation sweep extended to every op incl. reviews, plus the
    task-review decision entry points
  - registration still exactly once for /control and ^ctl:

H. MT-ADMIN-34 — ADMIN SYSTEM FOUNDATION (35-42)
  - frozen module registry contract: unique bounded keys, exactly
    the module set, NO financial state / secrets / id fields
  - every implemented module delegates to ITS existing command
  - reserved modules answer a safe unavailable notice — no reads,
    no render, no claim of working functionality
  - ctl:refresh re-renders read-only; same centralized auth gate
  - foreign callback families (wd/dp/pm/mr/atw/mproof) still
    registered exactly once; this module builds no foreign payload
  - successful navigation answers with the /control back hint
  - sectioned dashboard layout; the users slot shows the
    authoritative db.count_users() total (MT-ADMIN-35 — the full
    user-management surface is covered in test_admin_users.py)

Temp databases only; no production destinations or balances are used.

Run:
    python3 -m pytest test_admin_control.py -v
"""

from __future__ import annotations

import dataclasses
import os
import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

import admin_control
import admin_review_queue
import db
import deposit_manual_review
import deposit_proof_admin
import deposit_proof_store
import deposit_store
import manual_task
import payment_method_admin
import payment_method_store
import platform_settings
import rate_admin
import rate_store
import wallet
import withdrawal_admin
import withdrawal_store

from admin_control import (
    DEPOSITS_HEADER,
    HEADER,
    MSG_ADMIN_ONLY,
    MSG_ERROR,
    MSG_INVALID,
    PAYMETHODS_HEADER,
    RATE_HEADER,
    TASKS_HEADER,
    USERS_HEADER,
    WITHDRAWALS_HEADER,
    build_dashboard_keyboard,
    build_dashboard_text,
    parse_callback,
)

from test_payment_methods import (
    _answered,
    _callback,
    _reply,
    _run,
    _update,
)
from test_admin_review_queue import ADMIN_A as INBOX_ADMIN_A
from test_admin_review_queue import QueueTestBase
from test_withdrawal_service import (
    ADMIN_ID,
    FUND,
    PM_DESTINATION,
    USER_DEST,
    _Base,
)

STRANGER = 999_999

# 32 hex chars + extension — matches deposit_proof_store's storage-key
# shape rule; used ONLY to prove such keys never reach dashboard text.
PROOF_KEY = "cafebabecafebabecafebabecafebabe.png"

ENV_SECRET = "CTL-ENV-SENTINEL-77aa"

# Every financial mutation entry point that must stay untouched.
MUTATION_SPY_TARGETS = (
    (wallet, "credit_units"),
    (wallet, "reserve"),
    (wallet, "release_units"),
    (wallet, "settle_units"),
    (rate_store, "set_rate"),
    (payment_method_store, "create_payment_method"),
    (payment_method_store, "update_payment_method"),
    (payment_method_store, "set_payment_method_active"),
    (payment_method_store, "set_payment_method_deposits_enabled"),
    (payment_method_store, "delete_payment_method"),
    (deposit_store, "mark_credited"),
    (deposit_proof_store, "mark_reviewed"),
    (deposit_manual_review, "approve"),
    (deposit_manual_review, "reject"),
    # Task-review decision entry points (MT-ADMIN-33: the reviews
    # button must only NAVIGATE — never decide).
    (admin_review_queue, "handle_review_callback"),
    (manual_task.ManualReviewService, "decide"),
)

FINANCIAL_TABLES = (
    "users",
    "tasks",
    "user_tasks",
    "task_submissions",
    "wallets",
    "ledger",
    "withdrawal_requests",
    "payment_methods",
    "deposit_requests",
    "deposit_proofs",
    "platform_settings",
    "current_rate",
)


# ── Shared fixture ────────────────────────────────────────────────────


class ControlTestBase(_Base):
    """MT-ADMIN-23 financial fixture (temp DB, ADMINS=[ADMIN_ID],
    seeded withdrawal settings + one active payment method) +
    control-center drivers."""

    # ── drivers ──────────────────────────────────────────────────

    def _cmd(self, actor_id: int = ADMIN_ID, chat_type: str = "private"):
        update = _update(actor_id, "/control", chat_type=chat_type)
        _run(admin_control.control_command(update, mock.MagicMock()))
        return update

    def _press(
        self,
        data,
        actor_id: int = ADMIN_ID,
        chat_type: str = "private",
        *,
        message_gone: bool = False,
        answer_side_effect=None,
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
        _run(admin_control.control_callback(update, mock.MagicMock()))
        return update

    def _buttons(self, markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]

    # ── seeds (all through EXISTING production interfaces) ───────

    def _seed_tasks(self) -> None:
        db.create_task(
            "مهمة نشطة أولى", "d", "telegram", 1,
            active=True, db_path=self.db_path,
        )
        db.create_task(
            "مهمة نشطة ثانية", "d", "telegram", 1,
            active=True, db_path=self.db_path,
        )
        db.create_task(
            "مهمة متوقفة", "d", "telegram", 1,
            active=False, db_path=self.db_path,
        )

    def _seed_rate(self, value: str = "48.5", now=None) -> None:
        rate_store.set_rate(
            value,
            admin_user_id=ADMIN_ID,
            db_path=self.db_path,
            now=now,
        )

    def _seed_deposit_proof(self, user_id: int = 501):
        self.add_user(user_id)
        payment_method_store.set_payment_method_deposits_enabled(
            self.pm.id, True, updated_by=ADMIN_ID, db_path=self.db_path
        )
        platform_settings.set_setting(
            platform_settings.MINIMUM_DEPOSIT_UNITS,
            1000,
            admin_user_id=ADMIN_ID,
            db_path=self.db_path,
        )
        deposit = deposit_store.create_deposit_request(
            user_id=user_id,
            payment_method_id=self.pm.id,
            amount=5000,
            db_path=self.db_path,
        )
        proof = deposit_proof_store.submit_proof(
            request_id=deposit.request_id,
            user_id=user_id,
            storage_key=PROOF_KEY,
            mime_type="image/png",
            size_bytes=2048,
            width=640,
            height=480,
            db_path=self.db_path,
        )
        return deposit, proof

    def _seed_pending_withdrawal(self, user_id: int = 501):
        self.add_user(user_id)
        self.fund(user_id, FUND)
        return self.create_request(user_id=user_id)

    def _dump_state(self) -> dict:
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
                for table in FINANCIAL_TABLES
            }
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════
# A. COMMAND AUTH (1-6)
# ══════════════════════════════════════════════════════════════════


class TestCommandAuth(ControlTestBase):

    def test_01_admin_can_open_control_center(self) -> None:
        """1. An admin's private /control renders every card."""
        update = self._cmd(actor_id=ADMIN_ID)
        text = _reply(update)
        self.assertIn(HEADER, text)
        for header in (
            TASKS_HEADER,
            WITHDRAWALS_HEADER,
            DEPOSITS_HEADER,
            PAYMETHODS_HEADER,
            RATE_HEADER,
            USERS_HEADER,
        ):
            self.assertIn(header, text)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)

    def test_02_non_admin_cannot_access_and_no_data_is_read(self) -> None:
        """2. Non-admins get the standard refusal BEFORE any read."""
        with mock.patch.object(admin_control, "collect_snapshot") as snap:
            update = self._cmd(actor_id=STRANGER)
        snap.assert_not_called()
        text = _reply(update)
        self.assertEqual(text, MSG_ADMIN_ONLY)
        self.assertNotIn(HEADER, text)

    def test_03_group_chat_is_silent(self) -> None:
        """3. Group invocation produces ZERO replies (convention)."""
        update = self._cmd(chat_type="supergroup")
        update.message.reply_text.assert_not_called()

    def test_04_channel_chat_is_silent(self) -> None:
        """4. Channel invocation produces ZERO replies."""
        update = self._cmd(chat_type="channel")
        update.message.reply_text.assert_not_called()

    def test_05_untrusted_identity_and_missing_message_stay_silent(
        self,
    ) -> None:
        """5. Untrusted/missing identities and messages die silently."""
        for bad_id in (None, 0, -7, True, "6175354851"):
            update = _update(ADMIN_ID, "/control")
            update.effective_user.id = bad_id
            _run(admin_control.control_command(update, mock.MagicMock()))
            update.message.reply_text.assert_not_called()

        update = _update(ADMIN_ID, "/control")
        update.message = None
        _run(admin_control.control_command(update, mock.MagicMock()))
        # No message to reply to — must not raise.

    def test_06_render_failure_shows_safe_error(self) -> None:
        """6. Unexpected failures degrade to safe text, no traceback."""
        with mock.patch.object(
            admin_control,
            "collect_snapshot",
            side_effect=RuntimeError("boom-internals"),
        ):
            update = self._cmd()
        text = _reply(update)
        self.assertEqual(text, MSG_ERROR)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("boom-internals", text)


# ══════════════════════════════════════════════════════════════════
# B. METRICS / SOURCES (7-16)
# ══════════════════════════════════════════════════════════════════


class TestMetrics(ControlTestBase):

    def test_07_withdrawal_count_from_repository(self) -> None:
        """7. Pending count comes from SqliteWithdrawalRepository."""
        self._seed_pending_withdrawal(501)
        self._seed_pending_withdrawal(502)
        text = _reply(self._cmd())
        self.assertIn("السحوبات المعلقة: 2", text)

        # The source is the existing repository — never custom SQL.
        with mock.patch.object(
            withdrawal_store, "SqliteWithdrawalRepository"
        ) as repo_cls:
            repo_cls.return_value.list_pending.return_value = [
                object(),
                object(),
                object(),
            ]
            text2 = _reply(self._cmd())
        repo_cls.assert_called_once()
        self.assertIn("السحوبات المعلقة: 3", text2)

    def test_08_deposit_count_from_proof_store(self) -> None:
        """8. Pending count comes from deposit_proof_store (existing)."""
        self._seed_deposit_proof(501)
        self._seed_deposit_proof(502)
        text = _reply(self._cmd())
        self.assertIn("إثباتات الدفع: 2", text)
        # No authoritative list-query exists for raw pending deposits —
        # the aggregate is omitted rather than invented.
        self.assertNotIn("الإيداعات المعلقة", text)

        # The displayed number is exactly what the EXISTING reader
        # returns — the store is the source, never custom SQL.
        with mock.patch.object(
            deposit_proof_store,
            "list_pending_proofs",
            return_value=[object(), object(), object(), object()],
        ) as src:
            text2 = _reply(self._cmd())
        src.assert_called_once()
        self.assertIn("إثباتات الدفع: 4", text2)

    def test_08b_proof_count_uses_existing_reader(self) -> None:
        """8b. The displayed number is exactly what the store returns."""
        self._seed_deposit_proof(501)
        expected = len(deposit_proof_store.list_pending_proofs())
        self.assertEqual(expected, 1)
        text = _reply(self._cmd())
        self.assertIn(f"إثباتات الدفع: {expected}", text)

    def test_09_payment_method_counts_from_store(self) -> None:
        """9. Counts come from payment_method_store.list_payment_methods."""
        second = self.create_method(display_name="M2")
        payment_method_store.set_payment_method_deposits_enabled(
            self.pm.id, True, updated_by=ADMIN_ID, db_path=self.db_path
        )
        text = _reply(self._cmd())
        self.assertIn("طرق الدفع النشطة: 2", text)
        self.assertIn("طرق الإيداع المتاحة: 1", text)

        payment_method_store.set_payment_method_active(
            second.id, False, updated_by=ADMIN_ID, db_path=self.db_path
        )
        text2 = _reply(self._cmd())
        self.assertIn("طرق الدفع النشطة: 1", text2)
        self.assertIn("طرق الإيداع المتاحة: 1", text2)

    def test_10_rate_display_uses_get_current_quote(self) -> None:
        """10. Rate card (value + provider + state) is sourced from
        rate_store.get_current_quote — never built by hand."""
        with mock.patch.object(rate_store, "get_current_quote") as src:
            src.return_value = mock.Mock(
                rate_text="77.77", provider="manual"
            )
            text = _reply(self._cmd())
        src.assert_called_once()
        self.assertIn("77.77 USDT/EGP", text)
        self.assertIn("المصدر: manual", text)
        self.assertIn("الحالة: صالح", text)

    def test_11_fresh_rate_renders_canonical_quote(self) -> None:
        """11. A fresh persisted rate renders value + provider + state
        exactly from the authoritative quote."""
        self._seed_rate("48.5")
        text = _reply(self._cmd())
        self.assertIn("48.5 USDT/EGP", text)
        self.assertIn("المصدر: manual", text)
        self.assertIn("الحالة: صالح", text)

    def test_13b_no_rate_read_from_platform_settings(self) -> None:
        """13. The rate NEVER comes from platform_settings — the module
        never references it and rendering consults no setting."""
        source = open(admin_control.__file__, encoding="utf-8").read()
        self.assertIsNone(re.search(r"platform_settings\s*\.", source))

        self._seed_rate("48.5")
        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_setting, mock.patch.object(
            platform_settings, "get_required_setting"
        ) as get_required:
            text = _reply(self._cmd())
        get_setting.assert_not_called()
        get_required.assert_not_called()
        self.assertIn("48.5 USDT/EGP", text)

    def test_12_missing_rate_is_safe_unavailable(self) -> None:
        """12. No rate ever set -> safe status, dashboard still up."""
        text = _reply(self._cmd())
        self.assertIn("USDT/EGP: غير متاح", text)
        self.assertIn(HEADER, text)

    def test_13_stale_rate_is_safe_unavailable(self) -> None:
        """13. A stale row -> safe status, never a crash/traceback."""
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        self._seed_rate("48.5", now=two_hours_ago)
        text = _reply(self._cmd())
        self.assertIn("USDT/EGP: غير متاح", text)
        self.assertIn(HEADER, text)

    def test_14_failing_metric_degrades_and_is_logged(self) -> None:
        """14. Broken metric -> غير متاح + an ERROR log (not silent)."""
        with mock.patch.object(
            deposit_proof_store,
            "list_pending_proofs",
            side_effect=RuntimeError("db exploded"),
        ):
            with self.assertLogs("admin_control", level="ERROR") as logs:
                text = _reply(self._cmd())
        self.assertIn("إثباتات الدفع: غير متاح", text)
        self.assertIn(HEADER, text)
        self.assertNotIn("db exploded", text)
        joined = "\n".join(logs.output)
        self.assertIn("deposits_pending_proofs", joined)

        # Unexpected rate-read failures degrade the same way.
        with mock.patch.object(
            rate_store,
            "get_current_quote",
            side_effect=sqlite3.OperationalError("no such table"),
        ):
            with self.assertLogs("admin_control", level="ERROR") as logs2:
                text2 = _reply(self._cmd())
        self.assertIn("USDT/EGP: غير متاح", text2)
        self.assertIn("rate", "\n".join(logs2.output))

    def test_15_task_counts_from_existing_reads(self) -> None:
        """15. Active tasks from db.list_tasks; pending review from
        the existing admin_review_queue reader."""
        self._seed_tasks()
        text = _reply(self._cmd())
        self.assertIn("المهام النشطة: 2", text)

        fake_claims = [object(), object(), object()]
        with mock.patch.object(
            admin_review_queue,
            "list_pending_manual_claims",
            return_value=fake_claims,
        ) as src:
            text2 = _reply(self._cmd())
        src.assert_called_once()
        self.assertIn("بانتظار المراجعة: 3", text2)

    def test_16_no_sql_and_no_invented_aggregates(self) -> None:
        """16. The module issues no SQL and invents no raw deposit
        aggregate; an empty snapshot degrades every metric safely."""
        source = open(admin_control.__file__, encoding="utf-8").read()
        statement = re.compile(
            r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|PRAGMA)\b"
        )
        self.assertIsNone(statement.search(source))

        text = build_dashboard_text({})
        self.assertIn(HEADER, text)
        self.assertIn("المهام النشطة: غير متاح", text)
        self.assertIn("بانتظار المراجعة: غير متاح", text)
        self.assertIn("السحوبات المعلقة: غير متاح", text)
        self.assertIn("إثباتات الدفع: غير متاح", text)
        self.assertIn("طرق الدفع النشطة: غير متاح", text)
        self.assertIn("USDT/EGP: غير متاح", text)
        self.assertNotIn("الإيداعات المعلقة", text)


# ══════════════════════════════════════════════════════════════════
# C. NAVIGATION (17-24)
# ══════════════════════════════════════════════════════════════════


class TestNavigation(ControlTestBase):

    def test_17_keyboard_carries_fixed_ctl_set(self) -> None:
        """17. Exactly the registry modules + refresh; payloads static."""
        data = self._buttons(build_dashboard_keyboard())
        self.assertEqual(
            sorted(data),
            [
                "ctl:admins",
                "ctl:broadcast",
                "ctl:deposits",
                "ctl:health",
                "ctl:logs",
                "ctl:paymethods",
                "ctl:rate",
                "ctl:refresh",
                "ctl:reviews",
                "ctl:rewards",
                "ctl:settings",
                "ctl:tasks",
                "ctl:users",
                "ctl:withdrawals",
            ],
        )
        for payload in data:
            self.assertRegex(payload, r"^ctl:[a-z]+$")

    def test_18_press_withdrawals_lands_on_existing_queue(self) -> None:
        """18. The button invokes the EXISTING /withdrawals surface."""
        self._seed_pending_withdrawal(501)
        update = self._press("ctl:withdrawals")
        reply = update.callback_query.message.reply_text
        self.assertTrue(reply.called)
        self.assertIn(withdrawal_admin.LIST_HEADER, reply.call_args[0][0])
        update.callback_query.answer.assert_awaited()

    def test_19_press_deposits_lands_on_existing_queue(self) -> None:
        """19. The button invokes the EXISTING /deposits surface."""
        self._seed_deposit_proof(501)
        update = self._press("ctl:deposits")
        reply = update.callback_query.message.reply_text
        self.assertTrue(reply.called)
        self.assertIn(deposit_proof_admin.LIST_HEADER, reply.call_args[0][0])

    def test_20_press_tasks_and_rate_land_on_existing_surfaces(
        self,
    ) -> None:
        """20. ctl:tasks -> /listtasks; ctl:rate -> /setrate."""
        self._seed_tasks()
        update = self._press("ctl:tasks")
        reply = update.callback_query.message.reply_text
        self.assertIn("قائمة المهام", reply.call_args[0][0])

        update2 = self._press("ctl:rate")
        reply2 = update2.callback_query.message.reply_text
        self.assertEqual(reply2.call_args[0][0], rate_admin.MSG_USAGE)

        update3 = self._press("ctl:paymethods")
        reply3 = update3.callback_query.message.reply_text
        expected = payment_method_admin.build_panel_text(
            payment_method_store.list_payment_methods()
        )
        self.assertEqual(reply3.call_args[0][0], expected)

    def test_21_delegated_handler_gets_real_identity_and_chat(
        self,
    ) -> None:
        """21. The shim carries the real actor, private chat and the
        existing command text — the target re-checks auth itself."""
        with mock.patch.object(
            withdrawal_admin, "withdrawals_command", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:withdrawals")
        target.assert_awaited_once()
        shim, _context = target.await_args[0]
        self.assertEqual(shim.effective_user.id, ADMIN_ID)
        self.assertEqual(shim.effective_chat.type, "private")
        self.assertEqual(shim.message.text, "/withdrawals")

    def test_22_non_admin_press_refused_target_never_invoked(
        self,
    ) -> None:
        """22. Callback authorization is re-checked server-side."""
        with mock.patch.object(
            withdrawal_admin, "withdrawals_command", new=mock.AsyncMock()
        ) as target:
            update = self._press("ctl:withdrawals", actor_id=STRANGER)
        target.assert_not_awaited()
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)

    def test_23_group_press_answered_with_no_data(self) -> None:
        """23. Group presses reveal nothing and invoke nothing."""
        with mock.patch.object(
            withdrawal_admin, "withdrawals_command", new=mock.AsyncMock()
        ) as target:
            update = self._press(
                "ctl:withdrawals", chat_type="supergroup"
            )
        target.assert_not_awaited()
        self.assertIsNone(_answered(update.callback_query))

    def test_24_malformed_stale_and_failing_presses_fail_safely(
        self,
    ) -> None:
        """24. Unknown ops, vanished messages, exploding targets and
        dead queries all degrade to safe answers — never tracebacks."""
        for data in ("ctl:", "ctl:evil", "ctl:tasks:501", "wd:list:x", ""):
            update = self._press(data)
            self.assertEqual(_answered(update.callback_query), MSG_INVALID)

        update = self._press(None)
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)

        update = self._press("ctl:withdrawals", message_gone=True)
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)

        with mock.patch.object(
            withdrawal_admin,
            "withdrawals_command",
            side_effect=RuntimeError("target exploded"),
        ):
            update = self._press("ctl:withdrawals")
        answer = _answered(update.callback_query)
        self.assertEqual(answer, MSG_ERROR)
        self.assertNotIn("target exploded", answer)

        # A stale (already-answered) query never breaks the handler.
        update = self._press(
            "ctl:rate", answer_side_effect=Exception("query too old")
        )
        self.assertTrue(update.callback_query.message.reply_text.called)

    # ── MT-ADMIN-33 operational extension ──────────────────────

    def test_17b_keyboard_follows_registry_sections(self) -> None:
        """MT-ADMIN-34: registry-driven rows — every module labeled,
        management section first, refresh closes the keyboard."""
        markup = build_dashboard_keyboard()
        data = self._buttons(markup)
        self.assertEqual(
            data,
            [
                "ctl:users",
                "ctl:tasks",
                "ctl:reviews",
                "ctl:withdrawals",
                "ctl:deposits",
                "ctl:paymethods",
                "ctl:rate",
                "ctl:rewards",
                "ctl:broadcast",
                "ctl:settings",
                "ctl:admins",
                "ctl:logs",
                "ctl:health",
                "ctl:refresh",
            ],
        )
        labels = [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]
        for module in admin_control.MODULES:
            self.assertIn(module.label, labels)
        self.assertIn("🔄 تحديث", labels)

    def test_19b_press_reviews_lands_on_existing_queue(self) -> None:
        """ctl:reviews invokes the EXISTING /reviews surface."""
        update = self._press("ctl:reviews")
        reply = update.callback_query.message.reply_text
        self.assertTrue(reply.called)
        self.assertEqual(
            reply.call_args[0][0], admin_review_queue.MSG_NO_REVIEWS
        )
        update.callback_query.answer.assert_awaited()

    def test_21b_reviews_shim_carries_identity_and_command(self) -> None:
        """The reviews delegation re-presents a private /reviews."""
        with mock.patch.object(
            admin_review_queue, "reviews_command", new=mock.AsyncMock()
        ) as target:
            self._press("ctl:reviews")
        target.assert_awaited_once()
        shim, _context = target.await_args[0]
        self.assertEqual(shim.effective_user.id, ADMIN_ID)
        self.assertEqual(shim.effective_chat.type, "private")
        self.assertEqual(shim.message.text, "/reviews")

    def test_22b_every_callback_rechecks_authorization(self) -> None:
        """5. EVERY ctl op re-checks admin before touching anything —
        non-admin and group presses never reach the target handler."""
        import bot as bot_mod

        targets = (
            ("tasks", bot_mod, "list_tasks"),
            ("reviews", admin_review_queue, "reviews_command"),
            ("withdrawals", withdrawal_admin, "withdrawals_command"),
            ("deposits", deposit_proof_admin, "deposits_command"),
            ("paymethods", payment_method_admin, "paymethods_command"),
            ("rate", rate_admin, "setrate_command"),
        )
        for op, module, name in targets:
            with mock.patch.object(
                module, name, new=mock.AsyncMock()
            ) as target:
                update = self._press(f"ctl:{op}", actor_id=STRANGER)
            target.assert_not_awaited()
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, op
            )

            with mock.patch.object(
                module, name, new=mock.AsyncMock()
            ) as target:
                update = self._press(f"ctl:{op}", chat_type="supergroup")
            target.assert_not_awaited()
            self.assertIsNone(_answered(update.callback_query), op)


# ══════════════════════════════════════════════════════════════════
# D. SAFETY / SECURITY (25-31)
# ══════════════════════════════════════════════════════════════════


class TestSafety(ControlTestBase):

    def test_25_opening_performs_no_transaction(self) -> None:
        """25. BEGIN/commit is NOT required to open the dashboard."""
        with mock.patch.object(db, "transaction") as txn:
            self._cmd()
        txn.assert_not_called()

    def test_26_opening_changes_no_financial_row(self) -> None:
        """26. Every financial table is byte-identical after opening."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        self._seed_tasks()
        before = self._dump_state()
        self._cmd()
        self.assertEqual(before, self._dump_state())

    def test_27_no_mutation_entry_point_on_open_or_press(self) -> None:
        """27. Wallet/ledger/deposit/withdrawal/pm/rate/task mutation
        entry points never run — not on open, not on any button."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        self._seed_pending_withdrawal(502)
        self._seed_tasks()

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
        for key in [m.key for m in admin_control.MODULES] + [
            admin_control.OP_REFRESH
        ]:
            self._press(f"ctl:{key}")

        txn.assert_not_called()
        for label, spy in spies.items():
            self.assertFalse(spy.called, f"{label} must never run")
        self.assertEqual(before, self._dump_state())

    def test_28_payloads_carry_only_fixed_identifiers(self) -> None:
        """28. Callback data never carries ids, input or secrets."""
        for key in [m.key for m in admin_control.MODULES] + [
            admin_control.OP_REFRESH
        ]:
            self.assertEqual(parse_callback(f"ctl:{key}"), key)
        for bad in (
            "ctl:",
            "ctl:tasks:501",
            "ctl:nope",
            "ctl:TASKS",
            "ctl:tasks ",
            "ctl:../../etc",
            "wd:list:x",
            None,
            5,
            True,
        ):
            self.assertIsNone(parse_callback(bad))

    def test_29_output_excludes_keys_destinations_and_env(self) -> None:
        """29. No storage key, private destination or env value can
        reach the dashboard text."""
        self._seed_deposit_proof(501)
        self._seed_rate()
        with mock.patch.dict(
            os.environ, {"ADMIN_CONTROL_TEST_SECRET": ENV_SECRET}
        ):
            update = self._cmd()
        text = _reply(update)
        self.assertIn(HEADER, text)
        self.assertNotIn(PROOF_KEY, text)          # proof storage key
        self.assertNotIn(PM_DESTINATION, text)     # platform destination
        self.assertNotIn(self.pm.destination, text)
        self.assertNotIn(ENV_SECRET, text)         # environment value

    def test_30_output_excludes_user_identity_and_balances(
        self,
    ) -> None:
        """30. No client-supplied identity, balance or user destination
        is ever shown."""
        self._seed_pending_withdrawal(501)
        text = _reply(self._cmd())
        self.assertIn(HEADER, text)
        self.assertNotIn("501", text)              # telegram user id
        self.assertNotIn("u501", text)             # username
        self.assertNotIn(str(FUND), text)          # wallet balance
        self.assertNotIn(USER_DEST, text)          # user destination

    def test_31_module_contains_no_sql(self) -> None:
        """31. Read-only by construction: existing interfaces only."""
        source = open(admin_control.__file__, encoding="utf-8").read()
        statement = re.compile(
            r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE|BEGIN|COMMIT|PRAGMA)\b"
        )
        self.assertIsNone(statement.search(source))


# ══════════════════════════════════════════════════════════════════
# E. REGISTRATION (32-34)
# ══════════════════════════════════════════════════════════════════


class TestBotRegistration(unittest.TestCase):
    """``/control`` and ``ctl:`` are wired into main() exactly once."""

    def _capture(self):
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

    def test_32_control_command_registered_exactly_once(self) -> None:
        """32. One CommandHandler("control") in group 0."""
        captured, _bot_mod = self._capture()
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

    def test_33_ctl_callback_registered_exactly_once(self) -> None:
        """33. One CallbackQueryHandler on ^ctl: in group 5."""
        captured, _bot_mod = self._capture()
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

    def test_34_single_control_literal_in_bot_source(self) -> None:
        """34. Exactly one "control" and one ^ctl: registration line."""
        _captured, bot_mod = self._capture()
        source = open(bot_mod.__file__, encoding="utf-8").read()
        self.assertEqual(source.count('"control"'), 1)
        self.assertEqual(source.count("^ctl:"), 1)


# ══════════════════════════════════════════════════════════════════
# H. MT-ADMIN-34 — ADMIN SYSTEM FOUNDATION (registry/slots/refresh)
# ══════════════════════════════════════════════════════════════════


class TestAdminSystemFoundation(ControlTestBase):
    """MT-ADMIN-34 — module registry, reserved slots, refresh, back
    navigation, sectioned layout and namespace integrity."""

    PLACEHOLDERS = (
        "rewards",
        "broadcast",
        "settings",
        "admins",
        "logs",
        "health",
    )

    def test_35_registry_contract(self) -> None:
        """35. Static frozen registry: unique bounded keys, exactly
        the module set, no financial state/secret/id fields."""
        keys = [m.key for m in admin_control.MODULES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            set(keys),
            {
                "users",
                "tasks",
                "reviews",
                "withdrawals",
                "deposits",
                "paymethods",
                "rate",
                "rewards",
                "broadcast",
                "settings",
                "admins",
                "logs",
                "health",
            },
        )
        self.assertEqual(
            {
                f.name
                for f in dataclasses.fields(admin_control.AdminModule)
            },
            {"key", "label", "description", "command"},
        )
        expected_commands = {
            "tasks": "/listtasks",
            "reviews": "/reviews",
            "withdrawals": "/withdrawals",
            "deposits": "/deposits",
            "paymethods": "/paymethods",
            "rate": "/setrate",
        }
        for module in admin_control.MODULES:
            self.assertEqual(
                module.command, expected_commands.get(module.key)
            )
            self.assertTrue(module.label)

    def test_36_implemented_modules_delegate_to_their_command(self) -> None:
        """7. Every implemented module navigates to ITS existing
        command with the real identity + private chat."""
        import bot as bot_mod

        target_map = {
            "tasks": (bot_mod, "list_tasks"),
            "reviews": (admin_review_queue, "reviews_command"),
            "withdrawals": (withdrawal_admin, "withdrawals_command"),
            "deposits": (deposit_proof_admin, "deposits_command"),
            "paymethods": (payment_method_admin, "paymethods_command"),
            "rate": (rate_admin, "setrate_command"),
        }
        implemented = {
            m.key for m in admin_control.MODULES if m.command is not None
        }
        self.assertEqual(implemented, set(target_map))

        for key, (module, name) in target_map.items():
            command = admin_control.MODULES_BY_KEY[key].command
            with mock.patch.object(
                module, name, new=mock.AsyncMock()
            ) as target:
                self._press(f"ctl:{key}")
            target.assert_awaited_once()
            shim = target.await_args[0][0]
            self.assertEqual(shim.message.text, command)
            self.assertEqual(shim.effective_user.id, ADMIN_ID)
            self.assertEqual(shim.effective_chat.type, "private")

    def test_37_reserved_modules_answer_unavailable_safely(self) -> None:
        """8. Reserved modules never claim to work: safe notice, no
        reads, no render — for admin, non-admin AND group presses."""
        with mock.patch.object(admin_control, "collect_snapshot") as snap:
            for key in self.PLACEHOLDERS:
                update = self._press(f"ctl:{key}")
                self.assertEqual(
                    _answered(update.callback_query),
                    admin_control.MSG_MODULE_UNAVAILABLE,
                    key,
                )
                update.callback_query.message.reply_text.assert_not_called()
                update.callback_query.edit_message_text.assert_not_called()
        snap.assert_not_called()

        for key in self.PLACEHOLDERS:
            update = self._press(f"ctl:{key}", actor_id=STRANGER)
            self.assertEqual(
                _answered(update.callback_query), MSG_ADMIN_ONLY, key
            )
            update = self._press(f"ctl:{key}", chat_type="supergroup")
            self.assertIsNone(_answered(update.callback_query), key)

    def test_38_refresh_rerenders_read_only(self) -> None:
        """ctl:refresh re-renders in place — reads only, no writes."""
        self._seed_tasks()
        before = self._dump_state()
        with mock.patch.object(db, "transaction") as txn:
            update = self._press("ctl:refresh")
        txn.assert_not_called()
        self.assertEqual(before, self._dump_state())

        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        text = edit.call_args[0][0]
        self.assertIn(HEADER, text)
        self.assertIn("المهام النشطة: 2", text)
        self.assertIsInstance(
            edit.call_args[1]["reply_markup"], InlineKeyboardMarkup
        )
        update.callback_query.answer.assert_awaited()

        # A stale edit degrades to a safe no-op — never a traceback.
        stale = _callback(ADMIN_ID, "ctl:refresh")
        stale.callback_query.message.reply_text = mock.AsyncMock()
        stale.callback_query.edit_message_text = mock.AsyncMock(
            side_effect=Exception("message too old")
        )
        _run(admin_control.control_callback(stale, mock.MagicMock()))
        stale.callback_query.answer.assert_awaited()

    def test_39_refresh_rechecks_authorization(self) -> None:
        """ctl:refresh goes through the same centralized gate."""
        update = self._press("ctl:refresh", actor_id=STRANGER)
        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()

        update = self._press("ctl:refresh", chat_type="channel")
        self.assertIsNone(_answered(update.callback_query))
        update.callback_query.edit_message_text.assert_not_called()

    def test_40_existing_callback_namespaces_untouched(self) -> None:
        """10-14. Foreign callback families stay registered exactly
        once in bot.py; this module builds no foreign payload."""
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

    def test_41_back_hint_after_successful_navigation(self) -> None:
        """§7: successful delegation points back to /control."""
        self._seed_pending_withdrawal(501)
        update = self._press("ctl:withdrawals")
        self.assertEqual(
            _answered(update.callback_query), admin_control.BACK_HINT
        )
        self.assertIn("/control", admin_control.BACK_HINT)

    def test_42_dashboard_layout_sections_and_users_slot(self) -> None:
        """§9/§11/§13: sectioned dashboard; the users slot carries the
        authoritative db.count_users() total (0 in this empty DB)."""
        text = _reply(self._cmd())
        self.assertIn(HEADER, text)
        self.assertIn(admin_control.SEPARATOR, text)
        self.assertIn(admin_control.OVERVIEW_HEADER, text)
        self.assertIn(
            f"{admin_control.ADMIN_SECTION} · "
            f"{admin_control.SYSTEM_SECTION}",
            text,
        )
        self.assertIn(f"{USERS_HEADER}: 0", text)
        self.assertLess(
            text.index(admin_control.OVERVIEW_HEADER),
            text.index(TASKS_HEADER),
        )


# ══════════════════════════════════════════════════════════════════
# F. PENDING REVIEW — real claim through the MT-ADMIN-04 fixture
# ══════════════════════════════════════════════════════════════════


class TestRealPendingReviewCount(QueueTestBase):
    """A genuinely pending manual claim is counted by the dashboard
    through the existing ``list_pending_manual_claims`` reader."""

    def test_real_pending_claim_counted(self) -> None:
        self.assertEqual(admin_review_queue.list_pending_manual_claims(), [])
        self._open_claim()
        pending = admin_review_queue.list_pending_manual_claims()
        self.assertEqual(len(pending), 1)

        update = _update(INBOX_ADMIN_A, "/control")
        _run(admin_control.control_command(update, mock.MagicMock()))
        text = _reply(update)
        self.assertIn(HEADER, text)
        self.assertIn(f"بانتظار المراجعة: {len(pending)}", text)
        expected_active = len(db.list_tasks(active_only=True))
        self.assertIn(f"المهام النشطة: {expected_active}", text)

    def test_reviews_navigation_shows_real_pending_claim(self) -> None:
        """MT-ADMIN-33: ctl:reviews lands on the REAL queue with the
        claim rendered by the existing surface — never a second
        review implementation."""
        self._open_claim()
        update = _callback(INBOX_ADMIN_A, "ctl:reviews")
        update.callback_query.message.reply_text = mock.AsyncMock()
        _run(admin_control.control_callback(update, mock.MagicMock()))
        reply = update.callback_query.message.reply_text
        text = reply.call_args[0][0]
        self.assertTrue(
            text.startswith(admin_review_queue.MSG_QUEUE_HEADER), text
        )
        # The REAL pending claim is rendered by the existing surface.
        self.assertIn("مهمة مراجعة تجريبية", text)


if __name__ == "__main__":
    unittest.main()
