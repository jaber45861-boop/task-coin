"""
Withdrawal Notification Tests
=============================

Phase 5 of the withdrawal flow: state-change notifications are
BEST-EFFORT SIDE EFFECTS around the already-committed financial
transaction — never inside it.

Covered:

- submission → admin notice (Mini App route AND Telegram command both
  schedule exactly one notice through the bound AdminNotifier, with
  safe facts only)
- failed submission → no notice
- scheduler/delivery failure → the committed withdrawal is untouched
  (no rollback, no duplicate mutation)
- admin decision → requester notified exactly once (completed /
  rejected), post-commit
- duplicate admin action → the CAS refuses it AND no second notice is
  sent
- a failing user notification never rolls back or re-runs a committed
  settlement/release

Run:
    python3 -m pytest test_withdrawal_notifications.py -q
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import withdrawal_admin
import withdrawal_notifications

from test_payment_methods import _answered, _callback, _edited, _run
from test_withdrawal_admin import AdminWithdrawalTestBase
from test_withdrawal_routes import (
    CASH_AMOUNT,
    FUND,
    PM_DESTINATION,
    USER_DEST,
    _count_withdrawals,
    _fund,
    _make_pm,
    _post,
    _seed_settings,
    _set_rate,
    _valid_cash_payload,
    _wallet_units,
    client,        # noqa: F401 — pytest fixture
    env,           # noqa: F401 — pytest fixture
)
from test_withdrawal_service import ADMIN_ID
from test_withdrawal_user import _RecordingNotifier, bound_inbox  # noqa: F401


def _drain(scheduled: list) -> None:
    """Run every scheduled coroutine to completion."""
    for coroutine in scheduled:
        asyncio.run(coroutine)


# ════════════════════════════════════════════════════════════════════
# A. Submission → admin notice (post-commit, best-effort)
# ════════════════════════════════════════════════════════════════════


class TestRouteSubmissionNotice:

    def test_create_schedules_exactly_one_safe_notice(
        self, client, env, bound_inbox
    ):
        notifier, scheduled = bound_inbox
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()

        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 200

        # Exactly one scheduled notice, strictly AFTER the commit.
        assert len(scheduled) == 1
        _drain(scheduled)

        assert len(notifier.texts) == 1
        text = notifier.texts[0]
        created = response.get_json()["request"]
        assert created["request_id"][:8] in text
        assert CASH_AMOUNT in text
        # Safe facts only — no destinations, no credentials, no
        # internal columns.
        assert PM_DESTINATION not in text
        assert USER_DEST not in text
        assert "destination" not in json.dumps({"text": text}).lower()

        # The financial state is committed and singular regardless.
        assert _count_withdrawals(env) == 1
        assert _wallet_units(env)[1] > 0  # hold present

    def test_failed_create_schedules_nothing(
        self, client, env, bound_inbox
    ):
        notifier, scheduled = bound_inbox
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()

        response = _post(
            client,
            {
                "payment_method_id": pm.id,
                "amount": "5",          # below minimum
                "user_destination": USER_DEST,
            },
        )
        assert response.status_code == 400
        assert scheduled == []
        assert notifier.texts == []
        assert _count_withdrawals(env) == 0

    def test_scheduler_failure_never_affects_the_create(
        self, client, env
    ):
        """A dead bot loop must not roll back or repeat the committed
        financial transaction — and the user still gets their 200."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()

        def dead_loop(coroutine):
            coroutine.close()
            raise RuntimeError("bot loop dead")

        withdrawal_notifications.bind(mock.MagicMock(), dead_loop)
        try:
            response = _post(client, _valid_cash_payload(pm.id))
            assert response.status_code == 200
        finally:
            withdrawal_notifications.unbind()

        assert _count_withdrawals(env) == 1
        available, held = _wallet_units(env)
        assert held > 0
        assert available == FUND - held

    def test_unbound_inbox_never_raises(self):
        withdrawal_notifications.unbind()
        withdrawal_notifications.notify_submission(
            mock.MagicMock(request_id="req-x")
        )  # no exception = pass


# ════════════════════════════════════════════════════════════════════
# B. Admin decision → requester notice (exactly once, post-commit)
# ════════════════════════════════════════════════════════════════════


class TestUserDecisionNotice(AdminWithdrawalTestBase):
    """Drives the REAL admin callbacks (→ WithdrawalService) with a
    recording context and asserts the requester notice rules."""

    def _context(self, *, fail: bool = False):
        context = mock.MagicMock()
        if fail:
            context.bot.send_message = mock.AsyncMock(
                side_effect=RuntimeError("telegram down")
            )
        else:
            context.bot.send_message = mock.AsyncMock()
        return context

    def _press_ctx(self, data: str, context, actor_id: int = ADMIN_ID):
        update = _callback(actor_id, data)
        _run(withdrawal_admin.withdrawal_callback(update, context))
        return update

    def test_complete_notifies_requester_exactly_once(self):
        context = self._context()
        request = self._pending(user_id=501)

        self._press_ctx(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            ),
            context,
        )

        send = context.bot.send_message
        assert send.await_count == 1
        kwargs = send.await_args.kwargs
        assert kwargs["chat_id"] == 501
        text = kwargs["text"]
        assert "تم إتمام" in text
        assert request.request_id[:8] in text
        # Safe facts only.
        assert USER_DEST not in text

        # Financial state committed exactly once alongside the notice.
        assert self.raw_row(request.request_id)["status"] == "completed"
        assert len(self._settlement_rows(request.request_id)) == 1
        assert self.wallet_state(501)[1] == 0

    def test_reject_notifies_requester_exactly_once(self):
        context = self._context()
        request = self._pending(user_id=501)

        self._press_ctx(
            withdrawal_admin.do_reject_callback_data(
                request.request_id
            ),
            context,
        )

        send = context.bot.send_message
        assert send.await_count == 1
        kwargs = send.await_args.kwargs
        assert kwargs["chat_id"] == 501
        assert "رفض" in kwargs["text"]
        assert request.request_id[:8] in kwargs["text"]

        assert self.raw_row(request.request_id)["status"] == "rejected"
        assert len(self._release_rows(request.request_id)) == 1
        # Released exactly once — funds fully back.
        assert self.wallet_state(501) == (FUND, 0)

    def test_duplicate_complete_sends_no_second_notice(self):
        context = self._context()
        request = self._pending(user_id=501)
        complete = withdrawal_admin.do_complete_callback_data(
            request.request_id
        )

        self._press_ctx(complete, context)
        second = self._press_ctx(complete, context)

        # First: settled + notified.  Second: CAS refused, silent to
        # the requester, nothing settled twice.
        assert context.bot.send_message.await_count == 1
        assert self.raw_row(request.request_id)["status"] == "completed"
        assert len(self._settlement_rows(request.request_id)) == 1
        assert _answered(second.callback_query) == (
            withdrawal_admin.MSG_STATE_CHANGED
        )

    def test_duplicate_reject_sends_no_second_notice(self):
        context = self._context()
        request = self._pending(user_id=501)
        reject = withdrawal_admin.do_reject_callback_data(
            request.request_id
        )

        self._press_ctx(reject, context)
        self._press_ctx(reject, context)

        assert context.bot.send_message.await_count == 1
        assert self.raw_row(request.request_id)["status"] == "rejected"
        assert len(self._release_rows(request.request_id)) == 1

    def test_notify_failure_never_rolls_back_or_duplicates(self):
        """The requester notice failing (Telegram down) must not
        undo, repeat or re-run the committed settlement."""
        context = self._context(fail=True)
        request = self._pending(user_id=501)

        update = self._press_ctx(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            ),
            context,
        )

        # The send raised… but the admin still got the success card
        # (the catch-all never fired) and money moved exactly once.
        assert context.bot.send_message.await_count == 1
        assert "تمت إتمام" in _edited(update.callback_query)
        assert self.raw_row(request.request_id)["status"] == "completed"
        assert len(self._settlement_rows(request.request_id)) == 1
        available, held = self.wallet_state(501)
        assert held == 0
        assert available == FUND - int(request.wallet_debit_units)

    def test_non_admin_gets_no_notice_and_no_mutation(self):
        context = self._context()
        request = self._pending(user_id=501)

        update = self._press_ctx(
            withdrawal_admin.do_complete_callback_data(
                request.request_id
            ),
            context,
            actor_id=501,          # the requester himself
        )

        assert context.bot.send_message.await_count == 0
        assert self.raw_row(request.request_id)["status"] == "pending"
        assert self._settlement_rows(request.request_id) == []
        assert _answered(update.callback_query) is not None
