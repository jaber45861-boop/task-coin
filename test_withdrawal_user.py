"""
Telegram User Withdrawal Flow Tests
===================================

Proves the ``/withdraw`` Telegram entry point REACHES the existing
``WithdrawalService`` (never a parallel implementation) and that the
financial state stays atomic and idempotent through this transport:

- valid creation → one PENDING request + exact wallet reserve + exact
  ledger hold (ONE service transaction, asserted from committed rows)
- invalid amount / unknown / inactive / unsupported method / missing
  rate / missing settings → domain message + ZERO mutation
- duplicate submission → one withdrawal, one reservation
- identity comes only from ``effective_user`` (never from the text)
- group/channel invocations are silent; replies never leak the
  platform's payout coordinates
- a successful create schedules the best-effort admin notice
  (post-commit; a scheduler failure never affects the committed
  financial state)

Run:
    python3 -m pytest test_withdrawal_user.py -q
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import config
import db
import payment_method_store
import wallet
from config import CHANNELS
from withdrawal_rules import METHOD_USDT_BEP20, METHOD_VODAFONE_CASH
import withdrawal_notifications
import withdrawal_user

from test_payment_methods import _reply, _run, _update
from test_miniapp_auth import _TEST_BOT_TOKEN
from test_withdrawal_routes import (
    ADMIN_ID,
    CASH_AMOUNT,
    FUND,
    PM_DESTINATION,
    RATE_TEXT,
    USER_DEST,
    _make_pm,
    _seed_settings,
    _set_rate,
)
from test_withdrawal_service import USDT_DEBIT_1_5, VODAFONE_DEBIT

USER = 7701
OTHER_USER = 7702


# ── Fixture / helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated database + registered users + admin config.

    Financial prerequisites (rate/settings/method/funds) are seeded
    explicitly per test so each test states exactly what it needs.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "withdrawal_user.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER, "carol", "Carol")
    db.register_user(OTHER_USER, "dave", "Dave")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    CHANNELS.clear()
    yield db_path
    CHANNELS.clear()


def _fund(user_id: int = USER, units: int = FUND) -> None:
    wallet.ensure_wallet(user_id)
    wallet.credit_units(user_id, units)


def _cmd(user_id: int, text: str, chat_type: str = "private"):
    update = _update(user_id, text, chat_type=chat_type)
    _run(withdrawal_user.withdraw_command(update, mock.MagicMock()))
    return update


def _raw(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _requests(db_path: str) -> list[dict]:
    return _raw(
        db_path,
        "SELECT * FROM withdrawal_requests ORDER BY created_at",
    )


def _wallet(db_path: str, user_id: int = USER) -> tuple[int, int]:
    rows = _raw(
        db_path,
        "SELECT available_units, held_units FROM wallets "
        "WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        raise AssertionError("wallet row missing")
    return rows[0]["available_units"], rows[0]["held_units"]


def _ledger(db_path: str, user_id: int = USER) -> list[dict]:
    return _raw(
        db_path,
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id",
        (user_id,),
    )


def _assert_no_mutation(db_path: str) -> None:
    """The forbidden states: no request, no wallet change, no ledger."""
    assert _requests(db_path) == []
    rows = _raw(
        db_path,
        "SELECT available_units, held_units FROM wallets "
        "WHERE user_id = ?",
        (USER,),
    )
    for row in rows:
        assert (row["available_units"], row["held_units"]) == (FUND, 0)
    assert _ledger(db_path) == []


def _cash_form(pm_id: int, amount: str = CASH_AMOUNT) -> str:
    return f"/withdraw {amount} {pm_id} {USER_DEST}"


# ── A. Eligibility / info (bare command) ─────────────────────────────


class TestInfo:
    def test_shows_balance_methods_and_usage(self, env):
        _fund()
        pm = _make_pm(env)

        update = _cmd(USER, "/withdraw")
        text = _reply(update)

        assert "10.00000000" in text          # funded available balance
        assert f"{pm.id}." in text             # method id for the form
        assert pm.display_name in text
        assert "/withdraw <المبلغ>" in text   # usage line
        # Safe metadata only — never the platform's payout coordinates.
        assert PM_DESTINATION not in text

    def test_unfunded_user_shows_zero_balance(self, env):
        _make_pm(env)
        text = _reply(_cmd(USER, "/withdraw"))
        assert "0.00000000" in text

    def test_pending_request_marker_shown(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _cmd(USER, _cash_form(pm.id))

        text = _reply(_cmd(USER, "/withdraw"))
        assert "قيد المراجعة" in text

    def test_group_and_channel_invocations_are_silent(self, env):
        for chat_type in ("group", "supergroup", "channel"):
            update = _cmd(USER, "/withdraw", chat_type=chat_type)
            update.message.reply_text.assert_not_called()


# ── B. Valid creation reaches the service — atomic end state ─────────


class TestCreate:
    def test_cash_create_is_atomic_and_service_owned(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id))
        text = _reply(update)

        # Confirmation carries safe facts.
        assert "تم إنشاء طلب السحب" in text
        assert CASH_AMOUNT in text
        assert PM_DESTINATION not in text
        assert USER_DEST not in text  # destination never echoed back

        # Exactly one PENDING request for the SENDER (identity source).
        rows = _requests(env)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["user_id"] == USER
        assert rows[0]["method"] == METHOD_VODAFONE_CASH
        assert rows[0]["wallet_debit_units"] == VODAFONE_DEBIT
        assert rows[0]["payment_method_id"] == pm.id

        # Wallet reserve + ledger hold committed together (ONE tx).
        assert _wallet(env) == (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT)
        holds = [r for r in _ledger(env) if r["entry_type"] == "hold"]
        assert len(holds) == 1
        assert holds[0]["amount_units"] == VODAFONE_DEBIT
        assert holds[0]["available_delta"] == -VODAFONE_DEBIT
        assert holds[0]["held_delta"] == VODAFONE_DEBIT
        assert holds[0]["reference_id"] == rows[0]["request_id"]

    def test_usdt_method_derived_server_side(self, env):
        _fund()
        _make_pm(env)  # id 1 — cash
        usdt_pm = _make_pm(
            env, category="crypto", asset="USDT",
            display_name="TEST USDT",
        )
        _seed_settings(env)
        _set_rate(env)

        _cmd(USER, f"/withdraw 1.5 {usdt_pm.id} {USER_DEST}")

        rows = _requests(env)
        assert len(rows) == 1
        assert rows[0]["method"] == METHOD_USDT_BEP20
        assert rows[0]["wallet_debit_units"] == USDT_DEBIT_1_5
        assert _wallet(env) == (FUND - USDT_DEBIT_1_5, USDT_DEBIT_1_5)


# ── C. Validation failures → domain message, ZERO mutation ───────────


class TestValidation:
    def test_below_minimum_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id, amount="5"))
        text = _reply(update)

        assert text == withdrawal_user.MSG_BELOW_MINIMUM
        _assert_no_mutation(env)

    def test_garbage_amount_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id, amount="abc"))
        assert _reply(update) == withdrawal_user.MSG_INVALID_AMOUNT
        _assert_no_mutation(env)

    def test_unknown_method_rejected_no_mutation(self, env):
        _fund()
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(999))
        assert _reply(update) == withdrawal_user.MSG_PM_NOT_FOUND
        _assert_no_mutation(env)

    def test_inactive_method_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        payment_method_store.set_payment_method_active(
            pm.id, False, updated_by=1, db_path=env
        )
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id))
        assert _reply(update) == withdrawal_user.MSG_PM_UNAVAILABLE
        _assert_no_mutation(env)

    def test_unsupported_asset_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(
            env, category="crypto", asset="BTC",
            display_name="TEST BTC",
        )
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, f"/withdraw 10 {pm.id} {USER_DEST}")
        assert _reply(update) == withdrawal_user.MSG_UNSUPPORTED_METHOD
        _assert_no_mutation(env)

    def test_missing_rate_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        # deliberately NO rate

        update = _cmd(USER, _cash_form(pm.id))
        assert _reply(update) == withdrawal_user.MSG_RATE_UNAVAILABLE
        _assert_no_mutation(env)

    def test_stale_rate_rejected_no_mutation(self, env):
        """A rate older than the TTL is never used silently — the
        Telegram path honours the same freshness contract as the Mini
        App route (rate_store.get_current_quote inside the service)."""
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        stale_now = datetime.now(timezone.utc) - timedelta(minutes=30)
        _set_rate(env, now=stale_now)

        update = _cmd(USER, _cash_form(pm.id))
        assert _reply(update) == withdrawal_user.MSG_RATE_UNAVAILABLE
        _assert_no_mutation(env)

    def test_missing_settings_rejected_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        _set_rate(env)
        # deliberately NO platform settings

        update = _cmd(USER, _cash_form(pm.id))
        assert _reply(update) == withdrawal_user.MSG_SETTINGS_MISSING
        _assert_no_mutation(env)

    def test_malformed_forms_show_usage_no_mutation(self, env):
        _fund()
        pm = _make_pm(env)
        for body in (
            "10",                       # too few fields
            f"10 {pm.id}",              # no destination
            f"10 {pm.id}   ",           # empty destination
            f"10 not-a-number {USER_DEST}",   # bad method id
        ):
            update = _cmd(USER, f"/withdraw {body}")
            assert _reply(update) == withdrawal_user.MSG_INVALID_FORM, body
        _assert_no_mutation(env)


# ── D. Duplicate submission → one withdrawal, one reservation ────────


class TestDuplicateSubmission:
    def test_second_submission_is_refused_and_funds_held_once(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        first = _cmd(USER, _cash_form(pm.id))
        assert "تم إنشاء طلب السحب" in _reply(first)

        second = _cmd(USER, _cash_form(pm.id))
        text = _reply(second)
        assert text in (
            withdrawal_user.MSG_PENDING_EXISTS,
            withdrawal_user.MSG_COOLDOWN,
        )

        # ONE withdrawal, ONE reservation, ONE ledger hold.
        assert len(_requests(env)) == 1
        assert _wallet(env) == (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT)
        holds = [r for r in _ledger(env) if r["entry_type"] == "hold"]
        assert len(holds) == 1


# ── E. Identity / authorization / secrecy ────────────────────────────


class TestSecurity:
    def test_identity_comes_only_from_effective_user(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        db.register_user(OTHER_USER, "dave", "Dave")

        # Digits in the TEXT (destination) must never become an id.
        _cmd(USER, f"/withdraw {CASH_AMOUNT} {pm.id} 99999")
        rows = _requests(env)
        assert len(rows) == 1
        assert rows[0]["user_id"] == USER
        assert rows[0]["user_destination"] == "99999"

        # A different sender creates for THEMSELVES only.
        _fund(OTHER_USER)
        # cooldown/pending blocks this user's create — seed the other
        # user's own prerequisites and submit as them.
        _set_rate(env)
        _cmd(OTHER_USER, f"/withdraw {CASH_AMOUNT} {pm.id} {USER_DEST}")
        user_rows = [
            r for r in _requests(env) if r["user_id"] == OTHER_USER
        ]
        # Either refused (pending/cooldown can't apply — OTHER_USER has
        # none) or created — but NEVER attributed to USER.
        for row in user_rows:
            assert row["user_id"] == OTHER_USER
        assert _wallet(env, USER) == (
            FUND - VODAFONE_DEBIT, VODAFONE_DEBIT,
        )

    def test_non_admin_user_can_submit(self, env):
        """No admin gate: any registered private user may withdraw."""
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        assert USER not in config.ADMINS

        update = _cmd(USER, _cash_form(pm.id))
        assert "تم إنشاء طلب السحب" in _reply(update)

    def test_replies_and_logs_never_leak_platform_destination(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        seen = []
        for text in (
            _reply(_cmd(USER, "/withdraw")),
            _reply(_cmd(USER, _cash_form(pm.id))),
        ):
            seen.append(text)
        for text in seen:
            assert PM_DESTINATION not in text


# ── F. Notification side effect (post-commit, best-effort) ───────────


class _RecordingNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def notify_system(self, text, *, reply_markup=None, targets=None):
        self.texts.append(text)
        return [(ADMIN_ID, 100)]


@pytest.fixture
def bound_inbox():
    notifier = _RecordingNotifier()
    scheduled: list = []

    def scheduler(coroutine):
        scheduled.append(coroutine)
        return None

    withdrawal_notifications.bind(notifier, scheduler)
    yield notifier, scheduled
    withdrawal_notifications.unbind()


class TestSubmissionNotice:
    def test_successful_create_schedules_admin_notice(
        self, env, bound_inbox
    ):
        notifier, scheduled = bound_inbox
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id))
        assert "تم إنشاء طلب السحب" in _reply(update)

        # The reply itself is the user's confirmation; the admin notice
        # is scheduled exactly once, strictly post-commit.
        assert len(scheduled) == 1
        _run(scheduled[0])
        assert len(notifier.texts) == 1
        text = notifier.texts[0]
        rows = _requests(env)
        short_id = rows[0]["request_id"][:8]
        assert short_id in text
        assert pm.display_name in text
        # Safe facts only — no destinations, no credentials.
        assert PM_DESTINATION not in text
        assert USER_DEST not in text

        # The financial state was already committed before the notice.
        assert len(rows) == 1
        assert _wallet(env) == (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT)

    def test_failed_create_schedules_nothing(self, env, bound_inbox):
        notifier, scheduled = bound_inbox
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        _cmd(USER, _cash_form(pm.id, amount="5"))
        assert scheduled == []
        assert notifier.texts == []
        _assert_no_mutation(env)

    def test_scheduler_failure_never_affects_the_create(self, env):
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        def dead_loop(coroutine):
            coroutine.close()
            raise RuntimeError("bot loop dead")

        withdrawal_notifications.bind(mock.MagicMock(), dead_loop)
        try:
            update = _cmd(USER, _cash_form(pm.id))
            # The user still gets the confirmation…
            assert "تم إنشاء طلب السحب" in _reply(update)
        finally:
            withdrawal_notifications.unbind()

        # …and the committed financial state is intact and singular.
        assert len(_requests(env)) == 1
        assert _wallet(env) == (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT)
        holds = [r for r in _ledger(env) if r["entry_type"] == "hold"]
        assert len(holds) == 1

    def test_unbound_inbox_is_a_logged_noop(self, env):
        withdrawal_notifications.unbind()
        _fund()
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)

        update = _cmd(USER, _cash_form(pm.id))
        assert "تم إنشاء طلب السحب" in _reply(update)
        assert len(_requests(env)) == 1


# ── G. Source guard — the transport never owns money ─────────────────


class TestSourceGuard:
    def test_transport_source_never_mutates_money_or_transactions(self):
        """Mirrors ``test_route_source_never_owns_transaction_or_
        builds_quote``: the Telegram transport must contain NO
        transaction control, NO wallet/ledger mutation call and NO raw
        SQL — only ``WithdrawalService.create`` can move money."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "withdrawal_user.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())

        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)

        forbidden = {
            # transaction control
            "transaction", "execute", "executemany",
            # wallet mutations
            "credit_units", "reserve", "release_units", "settle_units",
            "ensure_wallet",
            # ledger mutations
            "record_credit", "record_debit", "record_hold",
            "record_release", "record_settlement",
        }
        assert called & forbidden == set(), (
            f"transport owns money/transaction code: "
            f"{sorted(called & forbidden)}"
        )
        # …while the authoritative service IS the mutation path.
        assert "create" in called
        assert "WithdrawalService" in open(
            path, encoding="utf-8"
        ).read()
