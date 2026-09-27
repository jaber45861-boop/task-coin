"""
Focused tests — Admin Withdrawal Review & Settlement (MT-ADMIN-27)
==================================================================

The admin-side review workflow for Mini App withdrawal requests:
``/withdrawals`` bounded pending queue → detail card → confirmation →
``WithdrawalService.complete`` / ``.reject`` (the SOLE mutation path).

Coverage required by MT-ADMIN-27 (40 tests):

A. ADMIN AUTH (1–4)
   1  admin can access the withdrawal list
   2  non-admin cannot access it
   3  admin callback is protected
   4  callback cannot be used by a non-admin

B. LIST (5–9)
   5  only pending withdrawals listed
   6  completed withdrawals excluded
   7  rejected withdrawals excluded
   8  list is bounded/paginated
   9  missing request handled safely

C. DETAIL (10–14)
   10 correct persisted facts displayed
   11 user destination displayed correctly to authorized admin
   12 platform destination is not confused with user destination
   13 callback data does not contain destination
   14 no sensitive data is unnecessarily logged

D. COMPLETE (15–22)
   15 confirmation required
   16 successful complete calls WithdrawalService.complete
   17 wallet settlement happens exactly once
   18 ledger settlement happens exactly once
   19 status becomes completed
   20 completed_at is set
   21 repeated completion is rejected safely
   22 stale admin screen cannot double-settle

E. REJECT (23–30)
   23 confirmation required
   24 successful reject calls WithdrawalService.reject
   25 wallet release happens exactly once
   26 ledger release happens exactly once
   27 status becomes rejected
   28 rejected_at is set
   29 repeated rejection is rejected safely
   30 stale admin screen cannot double-release

F. LEGACY / ERRORS (31–35)
   31 MissingWalletDebitError leaves row untouched
   32 RequestNotFoundError handled
   33 InvalidStateError handled
   34 insufficient held balance handled
   35 unexpected service error does not expose traceback

G. FINANCIAL IMMUTABILITY (36–40)
   36 complete does not read current fee/rate/minimum
   37 reject does not read current fee/rate/minimum
   38 persisted wallet_debit_units is the only settlement amount
   39 current platform settings cannot alter an existing request
   40 current RateQuote cannot alter an existing request

Temp databases only; no production destinations or balances are used.

Run:
    python3 -m pytest test_withdrawal_admin.py -v
"""

from __future__ import annotations

import re
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from telegram import InlineKeyboardMarkup

import platform_settings
import rate_store
import withdrawal_admin
from withdrawal_service import WithdrawalService

from test_payment_methods import (
    _answered,
    _callback,
    _edited,
    _markup_of_edit,
    _reply,
    _run,
    _update,
)
from test_withdrawal_service import (
    ADMIN_ID,
    FUND,
    NOW,
    PM_DESTINATION,
    USER_DEST,
    VODAFONE_DEBIT,
    _Base,
    _rate_derived_settings,
)

STRANGER = 999999

from withdrawal_admin import (  # isort: skip  (after fixtures)
    BTN_COMPLETE,
    BTN_CONFIRM_COMPLETE,
    BTN_CONFIRM_REJECT,
    BTN_REJECT,
    DETAIL_HEADER,
    LIST_HEADER,
    MSG_ADMIN_ONLY,
    MSG_INSUFFICIENT,
    MSG_INVALID,
    MSG_LEGACY,
    MSG_NOT_FOUND,
    MSG_STATE_CHANGED,
    MSG_ERROR,
    PAGE_SIZE,
    CONFIRM_COMPLETE_HEADER,
    CONFIRM_REJECT_HEADER,
)

_CB_RE = re.compile(r"^wd:(page|view|askc|askr|doc|dor):[A-Za-z0-9_-]{1,64}$")


# ── Delegation spies (prove the handler calls the production service) ──


class _SpyService:
    """Delegates EVERY call to the real production service."""

    def __init__(self, real: WithdrawalService) -> None:
        self._real = real
        self.complete_calls: list[str] = []
        self.reject_calls: list[str] = []

    def complete(self, request_id, **kwargs):
        self.complete_calls.append(request_id)
        return self._real.complete(request_id, **kwargs)

    def reject(self, request_id, **kwargs):
        self.reject_calls.append(request_id)
        return self._real.reject(request_id, **kwargs)


class _RaceService:
    """Simulates a concurrent actor finishing the request BETWEEN the
    handler's pending verification and its service call — the service
    CAS must refuse the second mutation."""

    def __init__(self, real: WithdrawalService, once) -> None:
        self._real = real
        self._once = once
        self._fired = False

    def _fire(self) -> None:
        if not self._fired:
            self._fired = True
            self._once()

    def complete(self, request_id, **kwargs):
        self._fire()
        return self._real.complete(request_id, **kwargs)

    def reject(self, request_id, **kwargs):
        self._fire()
        return self._real.reject(request_id, **kwargs)


# ── Shared fixture ────────────────────────────────────────────────────


class AdminWithdrawalTestBase(_Base):
    """MT-ADMIN-23 fixture (temp DB, ADMINS=[ADMIN_ID], seeded
    settings, payment method, wallet helpers) + MT-ADMIN-27 drivers."""

    # ── data helpers ─────────────────────────────────────────────

    def _pending(self, user_id: int = 501, **kwargs):
        """One funded user with exactly one pending request."""
        self.add_user(user_id)
        self.fund(user_id, FUND)
        return self.create_request(user_id=user_id, **kwargs)

    def _short(self, request_id: str) -> str:
        return request_id[:8]

    def _settlement_rows(self, request_id: str) -> list:
        return [
            row
            for row in self.ledger_rows(request_id)
            if row["entry_type"] == "settlement"
        ]

    def _release_rows(self, request_id: str) -> list:
        return [
            row
            for row in self.ledger_rows(request_id)
            if row["entry_type"] == "release"
        ]

    # ── handler drivers ──────────────────────────────────────────

    def _cmd(self, actor_id: int = ADMIN_ID, chat_type: str = "private"):
        update = _update(actor_id, "/withdrawals", chat_type=chat_type)
        _run(withdrawal_admin.withdrawals_command(update, mock.MagicMock()))
        return update

    def _press(
        self, data: str, actor_id: int = ADMIN_ID, chat_type: str = "private"
    ):
        update = _callback(actor_id, data, chat_type=chat_type)
        _run(withdrawal_admin.withdrawal_callback(update, mock.MagicMock()))
        return update

    def _spy_service(self) -> _SpyService:
        spy = _SpyService(WithdrawalService(db_path=self.db_path))
        patcher = mock.patch.object(
            withdrawal_admin, "_service", return_value=spy
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def _race_service(self, once) -> None:
        race = _RaceService(WithdrawalService(db_path=self.db_path), once)
        patcher = mock.patch.object(
            withdrawal_admin, "_service", return_value=race
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _all_buttons(self, markup: InlineKeyboardMarkup) -> list[str]:
        return [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]


# ══════════════════════════════════════════════════════════════════
# A. ADMIN AUTH (1–4)
# ══════════════════════════════════════════════════════════════════


class TestAdminAuth(AdminWithdrawalTestBase):

    def test_01_admin_can_access_withdrawal_list(self) -> None:
        """1. An admin's private /withdrawals renders the queue."""
        request = self._pending()
        update = self._cmd(actor_id=ADMIN_ID)

        text = _reply(update)
        markup = update.message.reply_text.call_args[1]["reply_markup"]
        self.assertIn(LIST_HEADER, text)
        self.assertIn(self._short(request.request_id), text)
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertIn(
            withdrawal_admin.view_callback_data(request.request_id),
            self._all_buttons(markup),
        )

    def test_02_non_admin_cannot_access_list(self) -> None:
        """2. Non-admins get the admin-only refusal with ZERO queue
        data; group/channel invocations stay completely silent."""
        request = self._pending()

        update = self._cmd(actor_id=STRANGER)
        text = _reply(update)
        self.assertEqual(text, MSG_ADMIN_ONLY)
        self.assertNotIn(LIST_HEADER, text)
        self.assertNotIn(self._short(request.request_id), text)

        for chat_type in ("group", "supergroup", "channel"):
            with self.subTest(chat_type=chat_type):
                update = self._cmd(actor_id=ADMIN_ID, chat_type=chat_type)
                self.assertIsNone(update.message.reply_text.call_args)

    def test_03_admin_callback_is_protected(self) -> None:
        """3. The wd: callback renders the detail card for an admin in
        a private chat — and stays inert (no data) in a group."""
        request = self._pending()

        update = self._press(
            withdrawal_admin.view_callback_data(request.request_id)
        )
        text = _edited(update.callback_query)
        self.assertIn(DETAIL_HEADER, text)
        self.assertIn(request.request_id, text)

        grouped = self._press(
            withdrawal_admin.view_callback_data(request.request_id),
            chat_type="supergroup",
        )
        self.assertIsNone(_answered(grouped.callback_query))
        grouped.callback_query.edit_message_text.assert_not_called()

    def test_04_callback_cannot_be_used_by_non_admin(self) -> None:
        """4. A non-admin pressing a real complete callback is refused
        and NOTHING is mutated."""
        spy = self._spy_service()
        request = self._pending()
        before = self.wallet_state(501)

        update = self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            ),
            actor_id=STRANGER,
        )

        self.assertEqual(_answered(update.callback_query), MSG_ADMIN_ONLY)
        update.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(spy.complete_calls, [])
        self.assertEqual(self.raw_row(request.request_id)["status"],
                         "pending")
        self.assertEqual(self.wallet_state(501), before)


# ══════════════════════════════════════════════════════════════════
# B. LIST (5–9)
# ══════════════════════════════════════════════════════════════════


class TestList(AdminWithdrawalTestBase):

    def test_05_only_pending_withdrawals_listed(self) -> None:
        """5. A pending request appears in /withdrawals."""
        pending = self._pending(501)

        text = _reply(self._cmd())
        self.assertIn(LIST_HEADER, text)
        self.assertIn(self._short(pending.request_id), text)
        self.assertEqual(self.pending_count(), 1)

    def test_06_completed_withdrawals_excluded(self) -> None:
        """6. Completed requests never enter the queue."""
        pending = self._pending(501)
        self.add_user(502)
        self.fund(502, FUND)
        completed = self.create_request(user_id=502)
        self.svc.complete(completed.request_id, now=NOW)

        text = _reply(self._cmd())
        self.assertIn(self._short(pending.request_id), text)
        self.assertNotIn(self._short(completed.request_id), text)

    def test_07_rejected_withdrawals_excluded(self) -> None:
        """7. Rejected requests never enter the queue."""
        pending = self._pending(501)
        self.add_user(502)
        self.fund(502, FUND)
        rejected = self.create_request(user_id=502)
        self.svc.reject(rejected.request_id, now=NOW)

        text = _reply(self._cmd())
        self.assertIn(self._short(pending.request_id), text)
        self.assertNotIn(self._short(rejected.request_id), text)

    def test_08_list_is_bounded_and_paginated(self) -> None:
        """8. Exactly PAGE_SIZE entries render per page with clamped,
        deterministic navigation."""
        rids: list[str] = []
        for offset, user_id in enumerate(range(501, 508)):
            rids.append(
                self._pending(
                    user_id, now=NOW + timedelta(minutes=offset)
                ).request_id
            )

        update = self._cmd()
        text = _reply(update)
        markup = update.message.reply_text.call_args[1]["reply_markup"]

        self.assertIn("(1–5 من 7)", text)
        # Page 1 = the 5 NEWEST requests (created_at DESC).
        for rid in rids[2:]:
            self.assertIn(self._short(rid), text)
        for rid in rids[:2]:
            self.assertNotIn(self._short(rid), text)

        buttons = self._all_buttons(markup)
        self.assertEqual(
            len([b for b in buttons if b.startswith("wd:view:")]), PAGE_SIZE
        )
        self.assertIn("wd:page:2", buttons)
        self.assertNotIn("wd:page:1", buttons)

        # Page 2 renders exactly the remaining 2.
        update = self._press("wd:page:2")
        text2 = _edited(update.callback_query)
        markup2 = _markup_of_edit(update.callback_query)
        for rid in rids[:2]:
            self.assertIn(self._short(rid), text2)
        self.assertNotIn(self._short(rids[-1]), text2)
        buttons2 = self._all_buttons(markup2)
        self.assertIn("wd:page:1", buttons2)
        self.assertNotIn("wd:page:3", buttons2)

        # Out-of-range pages clamp; malformed payloads are rejected.
        update = self._press("wd:page:999")
        self.assertIn(self._short(rids[0]), _edited(update.callback_query))
        update = self._press("wd:page:abc")
        self.assertEqual(_answered(update.callback_query), MSG_INVALID)
        update.callback_query.edit_message_text.assert_not_called()

    def test_09_missing_request_handled_safely(self) -> None:
        """9. A stale/unknown request id from the list fails safely
        with the Arabic not-found message and no edit."""
        self._pending()
        update = self._press(
            withdrawal_admin.view_callback_data("ghost404")
        )
        self.assertEqual(_answered(update.callback_query), MSG_NOT_FOUND)
        update.callback_query.edit_message_text.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# C. DETAIL (10–14)
# ══════════════════════════════════════════════════════════════════


class TestDetail(AdminWithdrawalTestBase):

    def _open_detail(self):
        request = self._pending()
        update = self._press(
            withdrawal_admin.view_callback_data(request.request_id)
        )
        return request, _edited(update.callback_query), update

    def test_10_correct_persisted_facts_displayed(self) -> None:
        """10. The card shows the trusted persisted facts: id, user
        identity, method/asset/provider, amount, fee, debit, created
        time, status and the persisted rate facts."""
        request, text, _ = self._open_detail()

        self.assertIn(request.request_id, text)
        self.assertIn("501 — @u501", text)
        self.assertIn("TEST-METHOD", text)
        self.assertIn("الأصل: EGP", text)
        self.assertIn("المزود: TEST-PROVIDER", text)
        self.assertIn(f"{request.amount_egp} EGP", text)
        self.assertIn(f"{request.fee_egp} EGP", text)
        self.assertIn(str(VODAFONE_DEBIT), text)
        self.assertIn("2026-09-27", text)
        self.assertIn("⏳ قيد الانتظار", text)
        # Persisted rate facts (never recomputed).
        self.assertIn("48.5", text)
        self.assertIn("manual", text)
        self.assertIn("2026-09-27T09:59:00+00:00", text)

    def test_11_user_destination_displayed_to_authorized_admin(self) -> None:
        """11. The USER's payout destination is shown so the admin can
        pay it manually — only to an authorized admin."""
        request, text, _ = self._open_detail()
        self.assertIn(USER_DEST, text)
        self.assertIn("وجهة دفع المستخدم:", text)

        # The same detail press from a stranger shows NO destination.
        stranger = self._press(
            withdrawal_admin.view_callback_data(request.request_id),
            actor_id=STRANGER,
        )
        self.assertEqual(_answered(stranger.callback_query), MSG_ADMIN_ONLY)
        stranger.callback_query.edit_message_text.assert_not_called()

    def test_12_platform_destination_never_confused(self) -> None:
        """12. pm_destination (the platform's own destination) is
        NEVER rendered — only the user's destination."""
        request, text, _ = self._open_detail()

        self.assertIn(USER_DEST, text)
        self.assertNotIn(PM_DESTINATION, text)
        # Also absent from the list and the confirm card.
        list_text = _reply(self._cmd())
        self.assertNotIn(PM_DESTINATION, list_text)
        confirm = self._press(
            withdrawal_admin.ask_complete_callback_data(
                request.request_id
            )
        )
        self.assertNotIn(
            PM_DESTINATION, _edited(confirm.callback_query)
        )

    def test_13_callback_data_contains_no_destination(self) -> None:
        """13. Every callback payload is a bounded wd:<op>:<ref>
        lookup pointer — never a destination or financial blob."""
        request = self._pending()

        markups: list[InlineKeyboardMarkup] = []
        cmd = self._cmd()
        markups.append(
            cmd.message.reply_text.call_args[1]["reply_markup"]
        )
        for data in (
            withdrawal_admin.view_callback_data(request.request_id),
            withdrawal_admin.ask_complete_callback_data(
                request.request_id
            ),
            withdrawal_admin.ask_reject_callback_data(request.request_id),
        ):
            press = self._press(data)
            markups.append(_markup_of_edit(press.callback_query))

        for markup in markups:
            for callback_data in self._all_buttons(markup):
                self.assertRegex(callback_data, _CB_RE)
                self.assertLessEqual(len(callback_data), 64)
                self.assertNotIn(USER_DEST, callback_data)
                self.assertNotIn(PM_DESTINATION, callback_data)
                self.assertNotIn(str(VODAFONE_DEBIT), callback_data)

    def test_14_no_sensitive_data_unnecessarily_logged(self) -> None:
        """14. Operational logging carries request id + action + admin
        + result — never either destination."""
        request = self._pending()

        with self.assertLogs("withdrawal_admin", level="DEBUG") as logs:
            self._press(
                withdrawal_admin.view_callback_data(request.request_id)
            )
            self._press(
                withdrawal_admin.do_complete_callback_data(
                    request.request_id
                )
            )

        joined = "\n".join(logs.output)
        self.assertIn(request.request_id, joined)
        self.assertIn(str(ADMIN_ID), joined)
        self.assertIn("complete", joined)
        self.assertNotIn(USER_DEST, joined)
        self.assertNotIn(PM_DESTINATION, joined)
        self.assertNotIn("TEST-PLATFORM-DEST", joined)


# ══════════════════════════════════════════════════════════════════
# D. COMPLETE (15–22)
# ══════════════════════════════════════════════════════════════════


class TestComplete(AdminWithdrawalTestBase):

    def test_15_confirmation_required(self) -> None:
        """15. The complete button only opens a confirmation card —
        the service is called exclusively by the confirm press."""
        spy = self._spy_service()
        request = self._pending()

        ask = self._press(
            withdrawal_admin.ask_complete_callback_data(
                request.request_id
            )
        )
        ask_text = _edited(ask.callback_query)
        ask_markup = _markup_of_edit(ask.callback_query)
        self.assertIn(CONFIRM_COMPLETE_HEADER, ask_text)
        self.assertIn("يدوياً", ask_text)  # manual payout assertion
        self.assertIn(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            ),
            self._all_buttons(ask_markup),
        )
        self.assertEqual(spy.complete_calls, [])
        self.assertEqual(
            self.raw_row(request.request_id)["status"], "pending"
        )

        confirm = self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )
        self.assertIn("تمت إتمام", _edited(confirm.callback_query))
        self.assertEqual(spy.complete_calls, [request.request_id])

    def test_16_successful_complete_calls_service(self) -> None:
        """16. A confirmed complete delegates to
        WithdrawalService.complete with the request id."""
        spy = self._spy_service()
        request = self._pending()

        update = self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )

        self.assertEqual(spy.complete_calls, [request.request_id])
        self.assertEqual(spy.reject_calls, [])
        self.assertIn("تمت إتمام", _edited(update.callback_query))

    def test_17_wallet_settlement_happens_exactly_once(self) -> None:
        """17. Held units drop by exactly the persisted debit once;
        available balance is never touched again."""
        request = self._pending()
        debit = self.raw_row(request.request_id)["wallet_debit_units"]
        self.assertEqual(debit, VODAFONE_DEBIT)
        self.assertEqual(
            self.wallet_state(501), (FUND - debit, debit)
        )

        self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )
        state = self.wallet_state(501)
        self.assertEqual(state, (FUND - debit, 0))

        # A repeat press changes nothing.
        self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )
        self.assertEqual(self.wallet_state(501), state)

    def test_18_ledger_settlement_happens_exactly_once(self) -> None:
        """18. Exactly ONE settlement entry exists for the request."""
        request = self._pending()
        rid = request.request_id
        self.assertEqual(len(self.ledger_rows(rid)), 1)  # the hold

        self._press(withdrawal_admin.do_complete_callback_data(rid))
        self.assertEqual(len(self._settlement_rows(rid)), 1)
        self.assertEqual(len(self.ledger_rows(rid)), 2)  # hold+settle

        self._press(withdrawal_admin.do_complete_callback_data(rid))
        self.assertEqual(len(self._settlement_rows(rid)), 1)
        self.assertEqual(len(self.ledger_rows(rid)), 2)

    def test_19_status_becomes_completed(self) -> None:
        """19. pending → completed via the service CAS."""
        request = self._pending()
        self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )
        row = self.raw_row(request.request_id)
        self.assertEqual(row["status"], "completed")

    def test_20_completed_at_is_set(self) -> None:
        """20. completed_at is stamped; rejected_at stays NULL."""
        request = self._pending()
        self._press(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            )
        )
        row = self.raw_row(request.request_id)
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNone(row["rejected_at"])

    def test_21_repeated_completion_rejected_safely(self) -> None:
        """21. Pressing complete again is refused safely — no second
        settlement, no state change."""
        request = self._pending()
        rid = request.request_id
        self._press(withdrawal_admin.do_complete_callback_data(rid))
        settled = self.wallet_state(501)

        repeat = self._press(withdrawal_admin.do_complete_callback_data(rid))
        self.assertEqual(
            _answered(repeat.callback_query), MSG_STATE_CHANGED
        )
        repeat.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(self.wallet_state(501), settled)
        self.assertEqual(len(self._settlement_rows(rid)), 1)
        self.assertEqual(self.raw_row(rid)["status"], "completed")

    def test_22_stale_screen_cannot_double_settle(self) -> None:
        """22. A stale confirmation card pressed AFTER the request
        completed elsewhere cannot settle twice — the service CAS is
        authoritative."""
        request = self._pending()
        rid = request.request_id
        # Another actor completes the request between the handler's
        # pending verification and its service call.
        self._race_service(
            lambda: self.svc.complete(rid, now=NOW)
        )

        stale = self._press(withdrawal_admin.do_complete_callback_data(rid))
        self.assertEqual(_answered(stale.callback_query), MSG_STATE_CHANGED)

        self.assertEqual(len(self._settlement_rows(rid)), 1)
        self.assertEqual(self.raw_row(rid)["status"], "completed")
        debit = self.raw_row(rid)["wallet_debit_units"]
        self.assertEqual(self.wallet_state(501), (FUND - debit, 0))


# ══════════════════════════════════════════════════════════════════
# E. REJECT (23–30)
# ══════════════════════════════════════════════════════════════════


class TestReject(AdminWithdrawalTestBase):

    def test_23_confirmation_required(self) -> None:
        """23. The reject button only opens a confirmation card — the
        service is called exclusively by the confirm press."""
        spy = self._spy_service()
        request = self._pending()

        ask = self._press(
            withdrawal_admin.ask_reject_callback_data(
                request.request_id
            )
        )
        ask_text = _edited(ask.callback_query)
        ask_markup = _markup_of_edit(ask.callback_query)
        self.assertIn(CONFIRM_REJECT_HEADER, ask_text)
        self.assertIn(
            withdrawal_admin.do_reject_callback_data(
                request.request_id
            ),
            self._all_buttons(ask_markup),
        )
        self.assertEqual(spy.reject_calls, [])
        self.assertEqual(
            self.raw_row(request.request_id)["status"], "pending"
        )

        confirm = self._press(
            withdrawal_admin.do_reject_callback_data(
                request.request_id
            )
        )
        self.assertIn("تم رفض", _edited(confirm.callback_query))
        self.assertEqual(spy.reject_calls, [request.request_id])

    def test_24_successful_reject_calls_service(self) -> None:
        """24. A confirmed reject delegates to
        WithdrawalService.reject with the request id."""
        spy = self._spy_service()
        request = self._pending()

        update = self._press(
            withdrawal_admin.do_reject_callback_data(
                request.request_id
            )
        )

        self.assertEqual(spy.reject_calls, [request.request_id])
        self.assertEqual(spy.complete_calls, [])
        self.assertIn("تم رفض", _edited(update.callback_query))

    def test_25_wallet_release_happens_exactly_once(self) -> None:
        """25. Held units return exactly once; a repeat press changes
        nothing."""
        request = self._pending()
        rid = request.request_id
        debit = self.raw_row(rid)["wallet_debit_units"]
        self.assertEqual(self.wallet_state(501), (FUND - debit, debit))

        self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(self.wallet_state(501), (FUND, 0))

        self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(self.wallet_state(501), (FUND, 0))

    def test_26_ledger_release_happens_exactly_once(self) -> None:
        """26. Exactly ONE release entry exists for the request."""
        request = self._pending()
        rid = request.request_id
        self.assertEqual(len(self.ledger_rows(rid)), 1)  # the hold

        self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(len(self._release_rows(rid)), 1)
        self.assertEqual(len(self.ledger_rows(rid)), 2)  # hold+release

        self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(len(self._release_rows(rid)), 1)
        self.assertEqual(len(self.ledger_rows(rid)), 2)

    def test_27_status_becomes_rejected(self) -> None:
        """27. pending → rejected via the service CAS."""
        request = self._pending()
        self._press(
            withdrawal_admin.do_reject_callback_data(request.request_id)
        )
        self.assertEqual(
            self.raw_row(request.request_id)["status"], "rejected"
        )

    def test_28_rejected_at_is_set(self) -> None:
        """28. rejected_at is stamped; completed_at stays NULL."""
        request = self._pending()
        self._press(
            withdrawal_admin.do_reject_callback_data(request.request_id)
        )
        row = self.raw_row(request.request_id)
        self.assertIsNotNone(row["rejected_at"])
        self.assertIsNone(row["completed_at"])

    def test_29_repeated_rejection_rejected_safely(self) -> None:
        """29. Pressing reject again is refused safely — no second
        release, no state change."""
        request = self._pending()
        rid = request.request_id
        self._press(withdrawal_admin.do_reject_callback_data(rid))
        released = self.wallet_state(501)

        repeat = self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(
            _answered(repeat.callback_query), MSG_STATE_CHANGED
        )
        repeat.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(self.wallet_state(501), released)
        self.assertEqual(len(self._release_rows(rid)), 1)
        self.assertEqual(self.raw_row(rid)["status"], "rejected")

    def test_30_stale_screen_cannot_double_release(self) -> None:
        """30. A stale confirmation card pressed AFTER the request
        was rejected elsewhere cannot release twice — the service CAS
        is authoritative."""
        request = self._pending()
        rid = request.request_id
        self._race_service(lambda: self.svc.reject(rid, now=NOW))

        stale = self._press(withdrawal_admin.do_reject_callback_data(rid))
        self.assertEqual(_answered(stale.callback_query), MSG_STATE_CHANGED)

        self.assertEqual(len(self._release_rows(rid)), 1)
        self.assertEqual(self.raw_row(rid)["status"], "rejected")
        self.assertEqual(self.wallet_state(501), (FUND, 0))


# ══════════════════════════════════════════════════════════════════
# F. LEGACY / ERRORS (31–35)
# ══════════════════════════════════════════════════════════════════


class TestLegacyAndErrors(AdminWithdrawalTestBase):

    def test_31_missing_wallet_debit_leaves_row_untouched(self) -> None:
        """31. A legacy NULL-debit row surfaces the safe operational
        error — never guessed, never mutated."""
        self.add_user(501)
        self.insert_legacy_null_debit("wd-legacy", user_id=501)
        before = self.raw_row("wd-legacy")

        update = self._press(
            withdrawal_admin.do_complete_callback_data("wd-legacy")
        )

        self.assertEqual(_answered(update.callback_query), MSG_LEGACY)
        update.callback_query.edit_message_text.assert_not_called()
        after = self.raw_row("wd-legacy")
        self.assertEqual(after["status"], "pending")
        self.assertIsNone(after["wallet_debit_units"])
        self.assertIsNone(after["completed_at"])
        self.assertIsNone(after["rejected_at"])
        self.assertEqual(dict(after), dict(before))
        self.assertEqual(self.ledger_rows("wd-legacy"), [])

        # The reject path behaves identically.
        reject = self._press(
            withdrawal_admin.do_reject_callback_data("wd-legacy")
        )
        self.assertEqual(_answered(reject.callback_query), MSG_LEGACY)
        self.assertEqual(dict(self.raw_row("wd-legacy")), dict(before))

    def test_32_request_not_found_handled(self) -> None:
        """32. Unknown request ids map to the Arabic not-found
        message — no traceback, no internals."""
        self._pending()
        update = self._press(
            withdrawal_admin.do_complete_callback_data("ghost404")
        )
        self.assertEqual(_answered(update.callback_query), MSG_NOT_FOUND)
        update.callback_query.edit_message_text.assert_not_called()

    def test_33_invalid_state_handled(self) -> None:
        """33. A request that turned non-pending between verification
        and the service call maps InvalidStateError to the safe Arabic
        message — no traceback."""
        request = self._pending()
        rid = request.request_id
        # Stale card + concurrent completion right before the CAS.
        self._race_service(lambda: self.svc.complete(rid, now=NOW))

        update = self._press(withdrawal_admin.do_complete_callback_data(rid))
        answer = _answered(update.callback_query)
        self.assertEqual(answer, MSG_STATE_CHANGED)
        self.assertNotIn("Traceback", answer)
        self.assertNotIn("InvalidStateError", answer)
        self.assertEqual(len(self._settlement_rows(rid)), 1)

    def test_34_insufficient_held_balance_handled(self) -> None:
        """34. Held balance that cannot cover the settlement maps to
        the safe Arabic message with the row left pending."""
        self.add_user(501)
        self.fund(501, FUND)  # available only, held = 0
        self.insert_legacy_big_debit("wd-big", user_id=501)

        update = self._press(
            withdrawal_admin.do_complete_callback_data("wd-big")
        )

        self.assertEqual(_answered(update.callback_query), MSG_INSUFFICIENT)
        update.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(self.raw_row("wd-big")["status"], "pending")
        self.assertEqual(self.ledger_rows("wd-big"), [])
        self.assertEqual(self.wallet_state(501), (FUND, 0))

    def test_35_unexpected_error_does_not_expose_traceback(self) -> None:
        """35. An unexpected service failure yields only the safe
        Arabic error — no traceback, no exception text, no internals."""
        request = self._pending()
        exploding = mock.Mock()
        exploding.complete.side_effect = RuntimeError(
            "SECRET_SQL_DETAIL: table withdrawal_requests exploded"
        )
        with mock.patch.object(
            withdrawal_admin, "_service", return_value=exploding
        ):
            update = self._press(
                withdrawal_admin.do_complete_callback_data(
                    request.request_id
                )
            )

        answer = _answered(update.callback_query)
        self.assertEqual(answer, MSG_ERROR)
        self.assertNotIn("SECRET_SQL_DETAIL", answer)
        self.assertNotIn("Traceback", answer)
        self.assertNotIn("RuntimeError", answer)
        self.assertNotIn("withdrawal_requests", answer)
        update.callback_query.edit_message_text.assert_not_called()
        self.assertEqual(
            self.raw_row(request.request_id)["status"], "pending"
        )


# ══════════════════════════════════════════════════════════════════
# G. FINANCIAL IMMUTABILITY (36–40)
# ══════════════════════════════════════════════════════════════════


class TestFinancialImmutability(AdminWithdrawalTestBase):

    def test_36_complete_does_not_read_current_settings(self) -> None:
        """36. The confirmed complete path never reads the current
        fee/rate/minimum platform settings."""
        request = self._pending()
        rid = request.request_id

        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_setting, mock.patch.object(
            platform_settings, "get_required_setting"
        ) as get_required:
            update = self._press(
                withdrawal_admin.do_complete_callback_data(rid)
            )

        self.assertIn("تمت إتمام", _edited(update.callback_query))
        get_setting.assert_not_called()
        get_required.assert_not_called()
        self.assertEqual(self.raw_row(rid)["status"], "completed")

    def test_37_reject_does_not_read_current_settings(self) -> None:
        """37. The confirmed reject path never reads the current
        fee/rate/minimum platform settings."""
        request = self._pending()
        rid = request.request_id

        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_setting, mock.patch.object(
            platform_settings, "get_required_setting"
        ) as get_required:
            update = self._press(
                withdrawal_admin.do_reject_callback_data(rid)
            )

        self.assertIn("تم رفض", _edited(update.callback_query))
        get_setting.assert_not_called()
        get_required.assert_not_called()
        self.assertEqual(self.raw_row(rid)["status"], "rejected")

    def test_38_persisted_debit_is_only_settlement_amount(self) -> None:
        """38. Settlement moves EXACTLY the persisted
        wallet_debit_units — nothing derived from current settings."""
        request = self._pending()
        rid = request.request_id
        debit = self.raw_row(rid)["wallet_debit_units"]
        self.assertEqual(debit, VODAFONE_DEBIT)
        before = self.wallet_state(501)

        # Change the current fee/minimum AFTER creation.
        self.seed_withdrawal_settings(
            *_rate_derived_settings(Decimal("100"))
        )

        self._press(withdrawal_admin.do_complete_callback_data(rid))

        after = self.wallet_state(501)
        # Available untouched; held dropped by exactly `debit`.
        self.assertEqual(after[0], before[0])
        self.assertEqual(before[1] - after[1], debit)
        self.assertEqual(after, (FUND - debit, 0))

    def test_39_current_settings_cannot_alter_existing(self) -> None:
        """39. Changed platform settings after creation have ZERO
        effect on an existing request's settlement."""
        request = self._pending()
        rid = request.request_id
        debit = self.raw_row(rid)["wallet_debit_units"]

        # New settings that WOULD imply a different debit at rate 100.
        self.seed_withdrawal_settings(
            *_rate_derived_settings(Decimal("100"))
        )
        changed_debit_at_rate_100 = Decimal("11") / Decimal("100")
        self.assertNotEqual(
            changed_debit_at_rate_100 * 100_000_000,
            Decimal(debit),  # sanity: the alternative really differs
        )

        with mock.patch.object(
            platform_settings, "get_setting"
        ) as get_setting:
            self._press(withdrawal_admin.do_complete_callback_data(rid))

        get_setting.assert_not_called()
        self.assertEqual(self.wallet_state(501), (FUND - debit, 0))
        self.assertEqual(self.raw_row(rid)["status"], "completed")

    def test_40_current_rate_quote_cannot_alter_existing(self) -> None:
        """40. A current RateQuote source can never alter an existing
        request's settlement — it is never consulted."""
        request = self._pending()
        rid = request.request_id
        debit = self.raw_row(rid)["wallet_debit_units"]

        with mock.patch.object(rate_store, "get_current_quote") as loader:
            update = self._press(
                withdrawal_admin.do_complete_callback_data(rid)
            )

        loader.assert_not_called()
        self.assertIn("تمت إتمام", _edited(update.callback_query))
        self.assertEqual(self.wallet_state(501), (FUND - debit, 0))
        self.assertEqual(len(self._settlement_rows(rid)), 1)


if __name__ == "__main__":
    unittest.main()
