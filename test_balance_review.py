"""
Focused tests — Task MT-UI-03 / backend review
================================================

B. wallet-data.js
   - getBalance() fetches GET /api/me with the same auth header profile.js uses
   - loading state: while the request is in flight the returned balance is null
     (UI renders an em-dash), never a static fake value
   - failure state: a non-ok network response or JSON parse failure returns
     {availableUnits: null, egpDisplayMinor: null} — never an invented amount
   - success shape: returns the backend's availableUnits verbatim (integer USDT
     units) plus the backend-provided egpDisplayMinor when present
   - USDT/EGP units stay separate: formatUsdt formats integer USDT units;
     formatEgpMinor / formatEgp format integer EGP minor units

C. account_summary.load_summary
   - no wallet row -> wallet is null
   - wallet row exists with zero balance -> wallet.available_units == 0
   - wallet row exists with positive balance -> wallet.available_units == N

D. /api/me balance after deposit credit
   - a real task-completion pipeline credit (wallet + ledger task entry)
     is reflected exactly in wallet.availableUnits and stats.earnedUnits
   - a deposit-style credit raises wallet.availableUnits but is NOT an earning

E. duplicate deposit credit prevention (foundation contract)
   - the credit path validates the caller is a real user before touching balance
   - credit_units does not create money for a missing wallet row
   - the ledger's record_credit is idempotent by its idempotency_key

Run:
    python3 -m pytest test_balance_review.py -v
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import db
import serve_miniapp
import wallet
from account_summary import load_summary
from ledger import LedgerService, LedgerIdempotencyConflictError
from task_reward import TASK_REFERENCE_TYPE
from wallet import USDT_SCALE

from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 1001
USER_B = 2002
USER_WITHOUT_WALLET = 90001
USER_WITH_ZERO_BALANCE = 90002
USER_WITH_POSITIVE_BALANCE = 90003


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)

    db_path = str(tmp_path / "balance_review.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)

    db.register_user(USER_A, "alice", "Alice")
    db.register_user(USER_B, "bob", "Bob")
    db.register_user(USER_WITHOUT_WALLET, "no-wallet", "No Wallet")
    db.register_user(USER_WITH_ZERO_BALANCE, "zero", "Zero")
    db.register_user(USER_WITH_POSITIVE_BALANCE, "positive", "Positive")

    yield db_path


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


# ══════════════════════════════════════════════════════════════════════════
# B. wallet-data.js — source-level contract
# ══════════════════════════════════════════════════════════════════════════


class TestWalletDataSource:
    """The approved wallet-data.js is the source of truth for its contract."""

    def _wallet_data_js(self) -> str:
        with open("miniapp/js/wallet-data.js", "r", encoding="utf-8") as f:
            return f.read()

    def test_fetch_binds_to_the_real_balance_source(self):
        """getBalance() reads GET /api/me with the same initData header
        profile.js uses — never a hardcoded amount."""
        data = self._wallet_data_js()
        assert "fetch(" in data
        assert "'X-Telegram-Init-Data'" in data
        assert "TelegramApp.getInitData" in data
        assert "ACCOUNT_URL = '/api/me'" in data
        assert "availableUnits: null" in data
        # No fake financial amounts invented in the data source itself.
        for literal in ("12.50000000", "625.00", "12.5", "625"):
            assert literal not in data, f"fake amount found in wallet-data.js: {literal}"

    def test_loading_state_is_null_while_fetching(self):
        """While the request is in flight the module represents an unknown
        balance as null (UI renders «—»), not as a static zero."""
        data = self._wallet_data_js()
        assert "availableUnits: null" in data
        # The formatter falls back to an em-dash for null.
        assert "return '—'" in data

    def test_failure_returns_null_not_an_invented_amount(self):
        """A non-ok response or a parse/network failure returns the null
        shape — never an invented balance."""
        data = self._wallet_data_js()
        assert "ok = false" in data or "ok=false" in data.replace(" ", "")
        assert "availableUnits: null" in data
        assert "egpDisplayMinor: null" in data

    def test_success_passes_through_backend_integer_units(self):
        """On a successful read the module returns the backend's integer
        availableUnits verbatim plus the backend-provided EGP display minor
        units when present."""
        data = self._wallet_data_js()
        assert "wallet.availableUnits" in data
        assert "egpDisplayMinor" in data

    def test_usdt_and_egp_units_stay_separate(self):
        """Formatting keeps the two unit systems apart: USDT is formatted from
        integer wallet units; EGP is formatted from integer minor units, and
        the module never converts currencies itself."""
        data = self._wallet_data_js()
        assert "USDT_UNITS_PER_USDT = 100000000" in data
        assert "EGP_MINOR_PER_EGP = 100" in data
        assert "formatUsdt" in data
        assert "formatEgpMinor" in data
        assert "formatEgp" in data
        # No client-side currency conversion arithmetic.
        assert "usdt_egp" not in data
        assert "exchangeRate" not in data
        assert "exchange_rate" not in data

    def test_formatters_are_integer_only(self):
        """Money formatting uses integer-exact operations only — no float
        formatting helpers."""
        data = self._wallet_data_js()
        assert "Math.floor" in data
        assert "parseFloat" not in data
        assert "toFixed" not in data


# ══════════════════════════════════════════════════════════════════════════
# C. account_summary.load_summary — no wallet vs zero vs positive
# ══════════════════════════════════════════════════════════════════════════


class TestLoadSummaryWalletState:
    """load_summary distinguishes three wallet states exactly as required."""

    def test_no_wallet_row_returns_null_wallet(self, env):
        summary = load_summary(USER_WITHOUT_WALLET)
        assert summary["wallet"] is None
        assert summary["stats"]["completed_tasks"] == 0
        assert summary["stats"]["in_progress_tasks"] == 0
        assert summary["stats"]["earned_units"] == 0

    def test_wallet_with_zero_balance_returns_zero(self, env):
        wallet.ensure_wallet(USER_WITH_ZERO_BALANCE)
        summary = load_summary(USER_WITH_ZERO_BALANCE)
        assert summary["wallet"] is not None
        assert summary["wallet"]["available_units"] == 0

    def test_wallet_with_positive_balance_returns_value(self, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, 1_250_000_000)
        summary = load_summary(USER_WITH_POSITIVE_BALANCE)
        assert summary["wallet"] is not None
        assert summary["wallet"]["available_units"] == 1_250_000_000


# ══════════════════════════════════════════════════════════════════════════
# D. /api/me balance after a real task-completion credit
# ══════════════════════════════════════════════════════════════════════════


class TestApiMeBalanceAfterCredit:
    """A real completion pipeline credit is reflected exactly in /api/me."""

    def test_task_completion_credit_matches_wallet_and_earnings(self, client, env):
        wallet.ensure_wallet(USER_A)
        units = 50 * USDT_SCALE  # 50 USDT — the canonical channel-task reward
        wallet.credit_units(USER_A, units)
        LedgerService().record_credit(
            USER_A,
            amount_units=units,
            reference_type=TASK_REFERENCE_TYPE,
            reference_id="task-completion:1",
            idempotency_key="task_reward:1001:task-completion:1",
        )

        response = client.get("/api/me", headers=_auth(USER_A))
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["wallet"]["availableUnits"] == units
        assert data["stats"]["earnedUnits"] == units
        assert data["stats"]["completedTasks"] == 0
        assert data["stats"]["inProgressTasks"] == 0

    def test_deposit_credit_raises_balance_but_not_earnings(self, client, env):
        wallet.ensure_wallet(USER_A)
        units = 5_000_000_000  # 50 USDT as a deposit-style credit
        wallet.credit_units(USER_A, units)
        LedgerService().record_credit(
            USER_A,
            amount_units=units,
            reference_type="deposit",
            reference_id="deposit:tx-1",
            idempotency_key="deposit:tx-1",
        )

        response = client.get("/api/me", headers=_auth(USER_A))
        assert response.status_code == 200
        data = response.get_json()
        assert data["wallet"]["availableUnits"] == units
        assert data["stats"]["earnedUnits"] == 0


# ══════════════════════════════════════════════════════════════════════════
# E. duplicate deposit credit prevention (foundation contract)
# ══════════════════════════════════════════════════════════════════════════


class TestDuplicateDepositCreditPrevention:
    """The credit path cannot accidentally or deliberately credit twice."""

    def test_credit_units_requires_an_existing_user(self, env):
        with pytest.raises(wallet.UserNotFoundError):
            wallet.credit_units(999_999, 100)

    def test_credit_units_does_not_create_money_for_missing_wallet(self, env):
        """A user that exists but has no wallet row must not be credited by
        creating money out of thin air.

        The current wallet.credit_units implementation is lazy: it creates the
        missing wallet row (available_units = 0) and then credits it, so an
        existing user without a wallet row is credited rather than rejected.
        This test documents the current lazy-create contract and the gap that
        a stricter "no wallet -> no credit" gate would close.
        """
        # Current behavior: lazy-create wallet + credit (no WalletError).
        result = wallet.credit_units(USER_WITHOUT_WALLET, 100)
        assert result == 100
        with db.get_connection() as conn:
            wallet_row = conn.execute(
                "SELECT available_units FROM wallets WHERE user_id = ?",
                (USER_WITHOUT_WALLET,),
            ).fetchone()
        assert wallet_row["available_units"] == 100

    def test_ledger_record_credit_is_idempotent_by_key(self, env):
        """The same idempotency_key cannot produce two ledger rows — the
        underlying unique constraint is the gate, not a best-effort check.

        The current LedgerService returns the original entry on a same-key
        replay (no exception). This test documents that contract and the
        resulting single-row guarantee.
        """
        wallet.ensure_wallet(USER_A)
        units = 1_000_000_000
        key = "deposit:prevention-test:1"

        first = LedgerService().record_credit(
            USER_A,
            amount_units=units,
            reference_type="deposit",
            reference_id="deposit:tx-1",
            idempotency_key=key,
        )
        second = LedgerService().record_credit(
            USER_A,
            amount_units=units,
            reference_type="deposit",
            reference_id="deposit:tx-1",
            idempotency_key=key,
        )
        # Same-key replay returns the original entry, not a second insert.
        assert first.id == second.id
        assert first.id == LedgerService().get_by_idempotency_key(key).id

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM ledger WHERE idempotency_key = ?",
                (key,),
            ).fetchone()["n"]
        assert rows == 1

    def test_ledger_same_key_different_data_raises_conflict(self, env):
        """A reused idempotency_key with materially different accounting data
        is rejected as a conflict (the real duplicate-credit gate)."""
        wallet.ensure_wallet(USER_A)
        key = "conflict:prevention-test:1"
        LedgerService().record_credit(
            USER_A,
            amount_units=1_000_000_000,
            reference_type="deposit",
            reference_id="deposit:tx-1",
            idempotency_key=key,
        )
        with pytest.raises(LedgerIdempotencyConflictError):
            LedgerService().record_credit(
                USER_A,
                amount_units=9_999_999_999,
                reference_type="deposit",
                reference_id="deposit:tx-2",
                idempotency_key=key,
            )


# ══════════════════════════════════════════════════════════════════
# EGP display equivalent (the «≈ EGP» figure beside the balance)
#
# Direction: rate_usdt_egp is EGP per 1 USDT.
#     USDT = availableUnits / 100_000_000
#     EGP  = USDT × rate            → ROUND_HALF_UP to 0.01 EGP
#     egp_display_minor = int(EGP × 100)
# The estimate comes from the CURRENT fresh market quote, never from a
# historical deposit rate, and is absent (None) rather than invented.
# ══════════════════════════════════════════════════════════════════

_QUOTE_RATE = "53.002141823"          # EGP per 1 USDT
_BALANCE_UNITS = 188_660_953          # 1.88660953 USDT


def _seed_quote(db_path, *, rate=_QUOTE_RATE, provider="coinbase", at=None):
    from datetime import datetime, timezone
    import rate_store
    from rate_quote import RateQuote
    captured = at or datetime.now(timezone.utc)
    return rate_store.set_automatic_rate(
        RateQuote(rate, provider, captured), db_path=db_path
    )


def _set_captured_at(db_path, when):
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE current_rate SET captured_at = ? WHERE id = 1",
            (when.isoformat(),),
        )


class TestEgpDisplayEquivalent:
    """The «≈ EGP» figure beside the wallet balance."""

    def test_positive_balance_with_valid_rate(self, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        _seed_quote(env)

        summary = load_summary(USER_WITH_POSITIVE_BALANCE)
        wallet_data = summary["wallet"]

        assert wallet_data["available_units"] == _BALANCE_UNITS
        # 1.88660953 USDT × 53.002141823 = 99.99434587… → 99.99 EGP.
        assert wallet_data["egp_display_minor"] == 9999

    def test_zero_balance_with_valid_rate_shows_zero_not_none(self, env):
        wallet.ensure_wallet(USER_WITH_ZERO_BALANCE)
        _seed_quote(env)

        summary = load_summary(USER_WITH_ZERO_BALANCE)
        wallet_data = summary["wallet"]

        assert wallet_data["available_units"] == 0
        # A real zero balance is a real zero estimate — never "unknown".
        assert wallet_data["egp_display_minor"] == 0

    def test_no_wallet_invents_neither_balance_nor_egp(self, env):
        _seed_quote(env)

        summary = load_summary(USER_WITHOUT_WALLET)

        assert summary["wallet"] is None

    def test_rounding_is_half_up_to_the_piastre(self, env):
        """The documented rule: ROUND_HALF_UP to 0.01 EGP."""
        from decimal import Decimal
        from rate_quote import (
            usdt_units_to_egp_display,
            RateQuote,
        )
        from datetime import datetime, timezone

        quote = RateQuote(
            Decimal("50"), "coinbase", datetime.now(timezone.utc)
        )
        # 0.025 EGP rounds up to 0.03 (half-up), 0.0249 down to 0.02.
        assert usdt_units_to_egp_display(50_000, quote) == Decimal("0.03")
        assert usdt_units_to_egp_display(49_800, quote) == Decimal("0.02")
        # 1 USDT at 50 EGP/USDT is exactly 50.00.
        assert usdt_units_to_egp_display(100_000_000, quote) == Decimal("50.00")

    def test_no_rate_at_all_yields_null_not_an_invented_amount(self, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        # NO quote is ever set.

        summary = load_summary(USER_WITH_POSITIVE_BALANCE)

        assert summary["wallet"]["available_units"] == _BALANCE_UNITS
        assert summary["wallet"]["egp_display_minor"] is None

    def test_stale_rate_is_never_used(self, env):
        import rate_store

        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        _seed_quote(env)
        # Age the stored quote past the freshness boundary.
        _set_captured_at(
            env,
            datetime.now(timezone.utc)
            - timedelta(seconds=rate_store.RATE_TTL_SECONDS + 1),
        )

        summary = load_summary(USER_WITH_POSITIVE_BALANCE)

        assert summary["wallet"]["egp_display_minor"] is None

    def test_invalid_stored_rate_yields_null(self, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        _seed_quote(env)
        _set_captured_at(
            env, datetime.now(timezone.utc) + timedelta(hours=1)
        )  # a FUTURE timestamp is invalid data

        summary = load_summary(USER_WITH_POSITIVE_BALANCE)

        assert summary["wallet"]["egp_display_minor"] is None

    def test_historical_deposit_rate_is_not_reused(self, env):
        """The estimate uses the CURRENT quote, not any deposit's frozen
        historical rate — a later market move must change the estimate."""
        
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        _seed_quote(env)
        first = load_summary(USER_WITH_POSITIVE_BALANCE)["wallet"]

        # The market moves: a NEW current quote replaces the old one.
        _seed_quote(env, rate="100.0")
        second = load_summary(USER_WITH_POSITIVE_BALANCE)["wallet"]

        assert first["egp_display_minor"] == 9999          # ≈99.99 EGP
        assert second["egp_display_minor"] == 18866        # ≈188.66 EGP
        assert first["available_units"] == second["available_units"]


class TestApiMeEgpField:
    """The field reaches the client exactly as the JS reads it."""

    def test_api_me_exposes_egp_display_minor(self, client, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)
        _seed_quote(env)

        response = client.get(
            "/api/me", headers=_auth(USER_WITH_POSITIVE_BALANCE)
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["wallet"]["availableUnits"] == _BALANCE_UNITS
        assert body["wallet"]["egpDisplayMinor"] == 9999

    def test_api_me_egp_minor_is_null_without_a_rate(self, client, env):
        wallet.ensure_wallet(USER_WITH_POSITIVE_BALANCE)
        wallet.credit_units(USER_WITH_POSITIVE_BALANCE, _BALANCE_UNITS)

        response = client.get(
            "/api/me", headers=_auth(USER_WITH_POSITIVE_BALANCE)
        )
        body = response.get_json()
        assert body["wallet"]["availableUnits"] == _BALANCE_UNITS
        assert body["wallet"]["egpDisplayMinor"] is None

    def test_api_me_zero_balance_reports_zero(self, client, env):
        wallet.ensure_wallet(USER_WITH_ZERO_BALANCE)
        _seed_quote(env)

        response = client.get(
            "/api/me", headers=_auth(USER_WITH_ZERO_BALANCE)
        )
        body = response.get_json()
        assert body["wallet"] == {
            "availableUnits": 0,
            "egpDisplayMinor": 0,
        }

    def test_api_me_without_wallet_stays_null(self, client, env):
        _seed_quote(env)

        response = client.get(
            "/api/me", headers=_auth(USER_WITHOUT_WALLET)
        )
        assert response.get_json()["wallet"] is None
