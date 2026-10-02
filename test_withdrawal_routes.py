"""
Focused tests for the user withdrawal flow (MT-ADMIN-25)
=========================================================

Covers the production wiring between the Mini App Wallet page and
``WithdrawalService``:

  Authentication (initData only)
      both endpoints require verified Telegram initData; invalid or
      missing auth → the existing 401 response; a browser-supplied
      ``user_id`` in the body is ignored (identity from initData only).

  GET /api/withdrawal/methods
      active methods only; EXACT safe field set (never
      ``destination``/``created_by``/``updated_by``/audit columns);
      empty list is valid.

  POST /api/withdrawal
      transport validation (shapes/types only, no finance policy);
      server-derived payout rail from the trusted payment-method row;
      cash + USDT success with exact wallet reserve, ledger hold and
      withdrawal persistence; response safety (no platform
      destination / admin columns).

  Rate authority (MT-ADMIN-26 → service)
      quote comes ONLY from ``rate_store`` server-side — missing /
      stale / future-crafted rates fail with a stable 503 and NO
      financial row; client-supplied rate fields are ignored; the
      route builds no quote and owns no transaction.

  Service quote_loader (additive path)
      the loader runs INSIDE the service's own ``BEGIN IMMEDIATE``
      transaction on that exact connection; an explicit quote still
      wins (MT-ADMIN-23 contract unchanged); neither → the original
      ``MissingRateError``; a stale loaded rate rolls everything back.

  Registration / UI wiring
      both blueprints registered on both servers; the Wallet السحب
      button delegates to the withdrawal module (which holds all the
      fetch logic — wallet.js keeps none).

Run:
    python3 -m pytest test_withdrawal_routes.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import db
import config
import payment_method_store
import platform_settings
import rate_store
import wallet
import withdrawal_rules
import serve_miniapp
from config import CHANNELS
from rate_quote import RateQuote
from withdrawal_rules import (
    METHOD_USDT_BEP20,
    METHOD_VODAFONE_CASH,
    MissingRateError,
)
from withdrawal_service import WithdrawalService

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 4401
ADMIN_ID = 900_025

RATE = Decimal("48.5")
RATE_TEXT = "48.5"

FUND = 1_000_000_000                     # 10 USDT
# 10 EGP + 1 EGP fee, ceiling @ 48.5 (approved MT-ADMIN-17 contract)
CASH_AMOUNT = "10"
CASH_DEBIT = 22_680_413
USDT_AMOUNT = "1.5"
USDT_DEBIT = 152_061_856                 # 150_000_000 + 2_061_856

PM_DESTINATION = "TEST-PLATFORM-DEST-25"
USER_DEST = "+20101234567 (TEST)"

_FORBIDDEN_RESPONSE_KEYS = {
    "destination",
    "pm_destination",
    "created_by",
    "updated_by",
    "sort_order",
}


# ── Fixtures / helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Environment + isolated database + admin configuration."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "withdrawal_routes.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    db.register_user(USER_A, "alice", "Alice")
    monkeypatch.setattr(config, "ADMINS", [ADMIN_ID])
    CHANNELS.clear()
    yield db_path
    CHANNELS.clear()


@pytest.fixture
def client(env):
    serve_miniapp.app.config["TESTING"] = True
    return serve_miniapp.app.test_client()


def _auth(user_id: int = USER_A) -> dict:
    return {INIT_DATA_HEADER: _make_init_data(user_id=user_id)}


def _make_pm(db_path: str, **overrides) -> payment_method_store.PaymentMethod:
    kwargs = dict(
        category="cash",
        display_name="TEST CASH",
        asset="EGP",
        provider="TEST-PROVIDER",
        destination=PM_DESTINATION,
        instructions="Test instructions",
        created_by=1,
        db_path=db_path,
    )
    kwargs.update(overrides)
    return payment_method_store.create_payment_method(**kwargs)


def _seed_settings(db_path: str, rate: Decimal = RATE) -> None:
    """Configure minimum/fee through the production settings contract."""
    minimum_units = wallet.decimal_to_units(
        withdrawal_rules.min_native_for(METHOD_USDT_BEP20, rate),
        field="minimum_withdrawal_units",
    )
    fee_units = wallet.decimal_to_units(
        withdrawal_rules.fee_native_for(METHOD_USDT_BEP20, rate),
        field="withdrawal_fee_units",
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for key, value in (
            (platform_settings.MINIMUM_WITHDRAWAL_UNITS, minimum_units),
            (platform_settings.WITHDRAWAL_FEE_UNITS, fee_units),
        ):
            if platform_settings.get_setting(key, conn=conn) == value:
                continue
            platform_settings.set_setting(
                key, value, admin_user_id=ADMIN_ID, conn=conn
            )
        conn.commit()
    finally:
        conn.close()


def _set_rate(db_path: str, text: str = RATE_TEXT, now=None) -> None:
    rate_store.set_rate(
        text, admin_user_id=ADMIN_ID, db_path=db_path, now=now
    )


def _fund(user_id: int = USER_A, units: int = FUND) -> int:
    wallet.ensure_wallet(user_id)
    return wallet.credit_units(user_id, units)


def _wallet_units(db_path: str, user_id: int = USER_A) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT available_units, held_units FROM wallets "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return (int(row["available_units"]), int(row["held_units"]))
    finally:
        conn.close()


def _count_withdrawals(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM withdrawal_requests"
        ).fetchone()[0]
    finally:
        conn.close()


def _withdrawal_row(db_path: str, request_id: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM withdrawal_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    finally:
        conn.close()


def _post(client, payload: dict, headers: dict | None = None):
    return client.post(
        "/api/withdrawal",
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers if headers is not None else _auth(),
    )


def _keys(obj) -> set:
    """Every response key at any depth (for safety scans)."""
    found = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(obj, list):
        for item in obj:
            found |= _keys(item)
    return found


def _valid_cash_payload(pm_id: int) -> dict:
    return {
        "payment_method_id": pm_id,
        "amount": CASH_AMOUNT,
        "user_destination": USER_DEST,
    }


# ════════════════════════════════════════════════════════════════════
# Authentication — identity only from verified initData
# ════════════════════════════════════════════════════════════════════


class TestAuthentication:

    def test_methods_requires_auth(self, client, env):
        response = client.get("/api/withdrawal/methods")
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

    def test_create_requires_auth(self, client, env):
        response = _post(client, {"payment_method_id": 1}, headers={})
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

    def test_invalid_init_data_rejected(self, client, env):
        headers = {INIT_DATA_HEADER: "user=forged&hash=bad&auth_date=1"}
        assert client.get(
            "/api/withdrawal/methods", headers=headers
        ).status_code == 401
        assert _post(
            client, {"payment_method_id": 1}, headers=headers
        ).status_code == 401

    def test_client_user_id_cannot_impersonate(self, client, env):
        """A body ``user_id`` is ignored — identity is initData only."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        payload = _valid_cash_payload(pm.id)
        payload["user_id"] = 999_999_999          # attacker-controlled
        payload["user"] = {"id": 999_999_999}      # ignored too
        response = _post(client, payload)
        assert response.status_code == 200
        request_id = response.get_json()["request"]["request_id"]
        row = _withdrawal_row(env, request_id)
        assert row["user_id"] == USER_A


# ════════════════════════════════════════════════════════════════════
# GET /api/withdrawal/methods — safe metadata, active only
# ════════════════════════════════════════════════════════════════════


class TestListMethods:

    def test_lists_only_active_methods(self, client, env):
        active = _make_pm(env, display_name="ACTIVE-CASH")
        inactive = _make_pm(env, display_name="INACTIVE-CASH")
        payment_method_store.set_payment_method_active(
            inactive.id, False, updated_by=1, db_path=env
        )
        _make_pm(
            env, category="crypto", asset="USDT",
            display_name="ACTIVE-CRYPTO", network="BEP-20",
        )
        response = client.get(
            "/api/withdrawal/methods", headers=_auth()
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        names = {m["display_name"] for m in data["methods"]}
        assert names == {"ACTIVE-CASH", "ACTIVE-CRYPTO"}

    def test_safe_fields_only(self, client, env):
        """Exact field set — never destination or admin audit columns."""
        _make_pm(env, network="TESTNET")
        response = client.get(
            "/api/withdrawal/methods", headers=_auth()
        )
        methods = response.get_json()["methods"]
        assert len(methods) == 1
        entry = methods[0]
        assert set(entry) == {
            "id", "category", "display_name", "asset",
            "network", "provider", "instructions",
        }
        assert not (_keys(entry) & _FORBIDDEN_RESPONSE_KEYS)
        assert PM_DESTINATION not in json.dumps(entry)

    def test_empty_list_is_valid(self, client, env):
        response = client.get(
            "/api/withdrawal/methods", headers=_auth()
        )
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "methods": []}


# ════════════════════════════════════════════════════════════════════
# POST /api/withdrawal — cash + USDT success paths
# ════════════════════════════════════════════════════════════════════


class TestCreateSuccess:

    def test_cash_create_success(self, client, env):
        pm = _make_pm(env, display_name="Vodafone TEST")
        _seed_settings(env)
        _set_rate(env)
        _fund()

        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        request = data["request"]
        assert request["method"] == METHOD_VODAFONE_CASH
        assert request["status"] == "pending"
        assert request["native_unit"] == "EGP"
        assert Decimal(request["amount"]) == Decimal(CASH_AMOUNT)
        assert request["payment_method_id"] == pm.id
        assert request["display_name"] == "Vodafone TEST"
        assert request["wallet_debit_units"] == CASH_DEBIT
        assert request["created_at"]

        # persisted facts: linked row, exact debit, manual rate pinned
        row = _withdrawal_row(env, request["request_id"])
        assert row["user_id"] == USER_A
        assert row["method"] == METHOD_VODAFONE_CASH
        assert row["status"] == "pending"
        assert row["wallet_debit_units"] == CASH_DEBIT
        assert row["payment_method_id"] == pm.id
        assert row["rate_provider"] == "manual"
        assert row["wallet_rate_usdt_egp"] == RATE_TEXT

        # wallet reserve + ledger hold happened atomically
        assert _wallet_units(env) == (FUND - CASH_DEBIT, CASH_DEBIT)
        conn = sqlite3.connect(env)
        try:
            hold = conn.execute(
                "SELECT held_delta FROM ledger WHERE reference_id = ?",
                (request["request_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert hold is not None and hold[0] > 0

    def test_usdt_create_success(self, client, env):
        pm = _make_pm(
            env, category="crypto", asset="USDT",
            display_name="USDT TEST", network="BEP-20",
        )
        _seed_settings(env)
        _set_rate(env)
        _fund()

        response = _post(client, {
            "payment_method_id": pm.id,
            "amount": USDT_AMOUNT,
            "user_destination": "0xDEADBEEF",
        })
        assert response.status_code == 200
        request = response.get_json()["request"]
        assert request["method"] == METHOD_USDT_BEP20
        assert request["native_unit"] == "USDT"
        assert Decimal(request["amount"]) == Decimal(USDT_AMOUNT)
        assert request["wallet_debit_units"] == USDT_DEBIT
        assert _wallet_units(env) == (FUND - USDT_DEBIT, USDT_DEBIT)

        row = _withdrawal_row(env, request["request_id"])
        assert row["method"] == METHOD_USDT_BEP20
        # payout-side exact rate columns (MT-ADMIN-19 contract)
        assert row["rate_usdt_egp"] == RATE_TEXT
        assert row["wallet_rate_usdt_egp"] == RATE_TEXT
        assert row["rate_provider"] == "manual"

    def test_response_carries_the_pinned_rate(self, client, env):
        """The rate block is the persisted rate_store quote — exact
        capture instant, never request-time and never client input."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        stored_captured_at = None
        conn = sqlite3.connect(env)
        try:
            stored_captured_at = conn.execute(
                "SELECT captured_at FROM current_rate WHERE id = 1"
            ).fetchone()[0]
        finally:
            conn.close()

        response = _post(client, _valid_cash_payload(pm.id))
        rate = response.get_json()["request"]["rate"]
        assert rate["usdt_egp"] == RATE_TEXT
        assert rate["provider"] == "manual"
        assert rate["captured_at"] == stored_captured_at

    def test_response_never_exposes_platform_destination(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        payload = response.get_json()
        assert not (_keys(payload) & _FORBIDDEN_RESPONSE_KEYS)
        assert PM_DESTINATION not in json.dumps(payload)
        # the user's own destination is not echoed back either
        assert "user_destination" not in _keys(payload)


# ════════════════════════════════════════════════════════════════════
# Rate authority — only rate_store, only fresh, server-side
# ════════════════════════════════════════════════════════════════════


class TestRateAuthority:

    def test_missing_rate_503_and_nothing_written(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)          # settings present, rate absent
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 503
        data = response.get_json()
        assert data["error"] == "rate_unavailable"
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)

    def test_stale_rate_503_and_nothing_written(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=rate_store.RATE_TTL_SECONDS + 100
        )
        _set_rate(env, now=stale_at)
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 503
        assert response.get_json()["error"] == "rate_unavailable"
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)

    def test_fresh_rate_within_ttl_accepted(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        fresh_at = datetime.now(timezone.utc) - timedelta(
            seconds=rate_store.RATE_TTL_SECONDS - 60
        )
        _set_rate(env, now=fresh_at)
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 200
        assert response.get_json()["request"]["rate"]["usdt_egp"] == RATE_TEXT

    def test_future_captured_at_rejected_503(self, client, env):
        """Invalid persisted data never counts as a fresh quote."""
        pm = _make_pm(env)
        _seed_settings(env)
        _fund()
        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        conn = sqlite3.connect(env)
        try:
            conn.execute(
                "INSERT INTO current_rate "
                "(id, rate_usdt_egp, provider, captured_at, "
                " updated_by, updated_at) "
                "VALUES (1, '48.5', 'manual', ?, ?, ?)",
                (future, ADMIN_ID, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 503
        assert response.get_json()["error"] == "rate_unavailable"
        assert _count_withdrawals(env) == 0

    def test_client_rate_fields_are_ignored(self, client, env):
        """No client-supplied rate/quote/provider can influence the
        authoritative quote — the server reads rate_store only."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env, "48.5")
        _fund()
        payload = _valid_cash_payload(pm.id)
        payload["rate_usdt_egp"] = "999"
        payload["quote"] = {"rate_usdt_egp": "999", "provider": "evil"}
        payload["rate_provider"] = "evil"
        payload["captured_at"] = "2000-01-01T00:00:00+00:00"
        response = _post(client, payload)
        assert response.status_code == 200
        request = response.get_json()["request"]
        assert request["rate"]["usdt_egp"] == "48.5"
        assert request["rate"]["provider"] == "manual"
        row = _withdrawal_row(env, request["request_id"])
        assert row["wallet_rate_usdt_egp"] == "48.5"
        assert row["rate_provider"] == "manual"

    def test_route_source_never_owns_transaction_or_builds_quote(self):
        """The HTTP layer opens no transaction and never constructs a
        RateQuote — it only injects rate_store.get_current_quote."""
        source = open(
            os.path.join(os.path.dirname(__file__), "withdrawal_routes.py"),
            encoding="utf-8",
        ).read()
        assert "RateQuote(" not in source
        assert "db.transaction" not in source
        assert "BEGIN IMMEDIATE" not in source
        assert "quote_loader=rate_store.get_current_quote" in source


# ════════════════════════════════════════════════════════════════════
# Transport validation — shapes/types only (no finance policy here)
# ════════════════════════════════════════════════════════════════════


class TestTransportValidation:

    def test_non_dict_body_rejected(self, client, env):
        for data in ('"text"', "[1,2]", "null"):
            response = client.post(
                "/api/withdrawal",
                data=data,
                content_type="application/json",
                headers=_auth(),
            )
            assert response.status_code == 400
            assert response.get_json()["error"] == "invalid_request"

    def test_missing_keys_rejected(self, client, env):
        cases = [
            {"amount": "10", "user_destination": USER_DEST},
            {"payment_method_id": 1, "user_destination": USER_DEST},
            {"payment_method_id": 1, "amount": "10"},
        ]
        for payload in cases:
            response = _post(client, payload)
            assert response.status_code == 400, payload
            assert response.get_json()["error"] == "invalid_request"

    def test_float_amount_rejected(self, client, env):
        """A JSON number (Python float) is never an authoritative
        amount — exact decimal text only."""
        pm = _make_pm(env)
        response = _post(client, {
            "payment_method_id": pm.id,
            "amount": 10.5,          # JSON float
            "user_destination": USER_DEST,
        })
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_amount"

    def test_malformed_amount_rejected(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        for bad in ("abc", "", "10.5.5"):
            response = _post(client, {
                "payment_method_id": pm.id,
                "amount": bad,
                "user_destination": USER_DEST,
            })
            assert response.status_code == 400, bad
            assert response.get_json()["error"] == "invalid_amount"
        assert _count_withdrawals(env) == 0

    def test_zero_and_negative_amount_rejected(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        for bad in ("0", "-5"):
            response = _post(client, {
                "payment_method_id": pm.id,
                "amount": bad,
                "user_destination": USER_DEST,
            })
            assert response.status_code == 400, bad
        assert _count_withdrawals(env) == 0

    def test_invalid_payment_method_id_types_rejected(self, client, env):
        _seed_settings(env)
        _set_rate(env)
        for bad in (True, "1", 0, -1, 1.5, None):
            response = _post(client, {
                "payment_method_id": bad,
                "amount": CASH_AMOUNT,
                "user_destination": USER_DEST,
            })
            assert response.status_code == 400, repr(bad)
            assert response.get_json()["error"] == "invalid_request"

    def test_destination_must_be_non_empty_text(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        for bad in (None, "", "   ", 123, ["x"]):
            response = _post(client, {
                "payment_method_id": pm.id,
                "amount": CASH_AMOUNT,
                "user_destination": bad,
            })
            assert response.status_code == 400, repr(bad)
            assert response.get_json()["error"] == "invalid_request"
        assert _count_withdrawals(env) == 0


# ════════════════════════════════════════════════════════════════════
# Payment-method resolution (server-derived payout rail)
# ════════════════════════════════════════════════════════════════════


class TestMethodResolution:

    def test_unknown_method_404(self, client, env):
        response = _post(client, _valid_cash_payload(999_999))
        assert response.status_code == 404
        assert response.get_json()["error"] == "payment_method_not_found"

    def test_inactive_method_409(self, client, env):
        pm = _make_pm(env)
        payment_method_store.set_payment_method_active(
            pm.id, False, updated_by=1, db_path=env
        )
        _seed_settings(env)
        _set_rate(env)
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 409
        assert (
            response.get_json()["error"] == "payment_method_unavailable"
        )
        assert _count_withdrawals(env) == 0

    def test_unsupported_asset_400(self, client, env):
        """A stored method outside the closed rail set cannot back a
        withdrawal (server-derived, not user-chosen)."""
        pm = _make_pm(
            env, category="crypto", asset="BTC", display_name="BTC"
        )
        _seed_settings(env)
        _set_rate(env)
        _fund()
        response = _post(client, {
            "payment_method_id": pm.id,
            "amount": USDT_AMOUNT,
            "user_destination": "bc1xyz",
        })
        assert response.status_code == 400
        assert response.get_json()["error"] == "unsupported_method"


# ════════════════════════════════════════════════════════════════════
# Business errors — domain → stable codes, atomic rollback
# ════════════════════════════════════════════════════════════════════


class TestBusinessErrors:

    def test_below_minimum_rejected(self, client, env):
        pm = _make_pm(env, category="crypto", asset="USDT")
        _seed_settings(env)
        _set_rate(env)
        _fund()
        response = _post(client, {
            "payment_method_id": pm.id,
            "amount": "0.001",         # far below the configured minimum
            "user_destination": "0xABC",
        })
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_amount"
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)

    def test_cooldown_409_with_retry_after(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        first = _post(client, _valid_cash_payload(pm.id))
        assert first.status_code == 200
        second = _post(client, _valid_cash_payload(pm.id))
        assert second.status_code == 409
        data = second.get_json()
        assert data["error"] == "cooldown"
        assert isinstance(data.get("retry_after_seconds"), int)
        assert data["retry_after_seconds"] > 0
        assert _count_withdrawals(env) == 1

    def test_insufficient_balance_409_wallet_unchanged(self, client, env):
        pm = _make_pm(env, category="crypto", asset="USDT")
        _seed_settings(env)
        _set_rate(env)
        _fund(units=1_000)              # far below amount+fee
        response = _post(client, {
            "payment_method_id": pm.id,
            "amount": USDT_AMOUNT,
            "user_destination": "0xABC",
        })
        assert response.status_code == 409
        assert response.get_json()["error"] == "insufficient_balance"
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (1_000, 0)

    def test_settings_missing_503(self, client, env):
        """No configured minimum/fee → deterministic configuration
        error, never a guessed fallback."""
        pm = _make_pm(env)
        _set_rate(env)
        _fund()
        response = _post(client, _valid_cash_payload(pm.id))
        assert response.status_code == 503
        assert (
            response.get_json()["error"] == "withdrawal_settings_missing"
        )
        assert _count_withdrawals(env) == 0


# ════════════════════════════════════════════════════════════════════
# Service-level quote_loader (additive same-transaction path)
# ════════════════════════════════════════════════════════════════════


class TestServiceQuoteLoader:
    """The loader participates in the ONE service-owned transaction."""

    def _service(self, db_path: str, loader=None) -> WithdrawalService:
        return WithdrawalService(db_path=db_path, quote_loader=loader)

    def _create(self, db_path: str, pm_id: int, loader=None, **overrides):
        kwargs = dict(
            payment_method_id=pm_id,
            user_destination=USER_DEST,
            now=datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc),
        )
        kwargs.update(overrides)
        return self._service(db_path, loader).create(
            USER_A, METHOD_VODAFONE_CASH, CASH_AMOUNT, **kwargs
        )

    def test_loader_reads_on_the_transaction_connection(self, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        seen = {}

        def loader(*, connection):
            seen["in_transaction"] = connection.in_transaction
            seen["quote"] = rate_store.get_current_quote(
                connection=connection
            )
            return seen["quote"]

        request = self._create(env, pm.id, loader=loader)
        # the loader ran INSIDE BEGIN IMMEDIATE on the txn connection
        assert seen["in_transaction"] is True
        assert seen["quote"].rate_text == RATE_TEXT
        # …and its quote is the one the financial facts pinned
        assert request.wallet_rate_usdt_egp == RATE_TEXT
        assert request.rate_provider == "manual"
        assert _wallet_units(env) == (FUND - CASH_DEBIT, CASH_DEBIT)

    def test_explicit_quote_still_wins(self, env):
        """MT-ADMIN-23 contract unchanged: an explicit quote is used
        as-is and the loader is never consulted."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()

        def loader(*, connection):
            raise AssertionError("loader must not run for an explicit quote")

        explicit = RateQuote(
            Decimal("48.5"), "manual",
            datetime(2026, 9, 27, 10, 0, 0, tzinfo=timezone.utc),
        )
        request = self._create(
            env, pm.id, loader=loader, quote=explicit
        )
        assert request.wallet_rate_usdt_egp == "48.5"
        assert _count_withdrawals(env) == 1

    def test_no_loader_and_no_quote_keeps_missing_rate_contract(self, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        with pytest.raises(MissingRateError):
            self._create(env, pm.id)     # loader=None, quote omitted
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)

    def test_stale_loaded_rate_rolls_back_everything(self, env):
        """The quote is loaded before any mutation; a stale rate fails
        the whole transaction — no row, no reserve, no hold."""
        from withdrawal_contract import translate_to_domain_error

        pm = _make_pm(env)
        _seed_settings(env)
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=rate_store.RATE_TTL_SECONDS + 5
        )
        _set_rate(env, now=stale_at)
        _fund()

        def loader(*, connection):
            return rate_store.get_current_quote(connection=connection)

        with pytest.raises(Exception) as excinfo:
            self._create(env, pm.id, loader=loader)
        mapped = translate_to_domain_error(excinfo.value)
        assert isinstance(mapped, withdrawal_rules.InvalidRateError)
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)

    def test_loader_failure_mid_transaction_leaves_no_partial_state(
        self, env
    ):
        """A loader that dies after opening the transaction still
        rolls back cleanly (the service owns commit/rollback)."""
        pm = _make_pm(env)
        _seed_settings(env)
        _fund()

        def loader(*, connection):
            raise rate_store.RateUnavailableError("no rate row")

        with pytest.raises(Exception):
            self._create(env, pm.id, loader=loader)
        assert _count_withdrawals(env) == 0
        assert _wallet_units(env) == (FUND, 0)


# ════════════════════════════════════════════════════════════════════
# Registration + Wallet UI wiring
# ════════════════════════════════════════════════════════════════════


class TestRegistrationAndUi:

    def test_blueprint_routes_registered(self, client, env):
        """Both endpoints exist (401, not 404, without auth)."""
        assert client.get(
            "/api/withdrawal/methods"
        ).status_code == 401
        assert client.post(
            "/api/withdrawal", json={}
        ).status_code == 401

    def test_single_entry_server_registers_blueprint(self):
        source = open(
            os.path.join(os.path.dirname(__file__), "bot.py"),
            encoding="utf-8",
        ).read()
        assert source.count(
            "from withdrawal_routes import withdrawal_bp"
        ) == 1
        assert source.count(
            "mini_app.register_blueprint(withdrawal_bp)"
        ) == 1

    def test_index_loads_withdrawal_module(self):
        html = open("miniapp/index.html", encoding="utf-8").read()
        assert '<script src="js/withdrawal.js"></script>' in html

    def test_wallet_withdraw_button_delegates_to_form(self):
        """The Wallet السحب button opens the form; wallet.js itself
        keeps no fetch (all API logic lives in withdrawal.js)."""
        wallet_js = open("miniapp/js/wallet.js", encoding="utf-8").read()
        assert "WithdrawalUI.open()" in wallet_js
        assert "fetch(" not in wallet_js
        ui = open("miniapp/js/withdrawal.js", encoding="utf-8").read()
        assert "return { open, close }" in ui
        assert INIT_DATA_HEADER in ui
        assert "'/api/withdrawal'" in ui


# ════════════════════════════════════════════════════════════════════
# GET /api/withdrawal/requests — the user's own status/list
# ════════════════════════════════════════════════════════════════════

class TestStatusList:
    """Authenticated read of the caller's OWN withdrawal requests —
    transport-only: initData identity, bounded repository read, the
    same safe payload as create, zero mutation."""

    def _get(self, client, query: str = "", headers: dict | None = None):
        return client.get(
            "/api/withdrawal/requests" + query,
            headers=headers if headers is not None else _auth(),
        )

    def test_requires_valid_init_data(self, client, env):
        assert self._get(client, headers={}).status_code == 401
        assert (
            self._get(
                client, headers={INIT_DATA_HEADER: "garbage"}
            ).status_code
            == 401
        )

    def test_empty_list_is_valid(self, client, env):
        response = self._get(client)
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["requests"] == []

    def test_client_user_id_param_cannot_impersonate(self, client, env):
        """Identity comes only from initData — a ``user_id`` query
        parameter can never select another user's rows."""
        other_id = 4499
        db.register_user(other_id, "mallory", "Mallory")

        # A request exists for USER_A…
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        assert _post(client, _valid_cash_payload(pm.id)).status_code == 200

        # …OTHER sees none, even when pointing at USER_A explicitly.
        response = self._get(
            client,
            query=f"?user_id={USER_A}",
            headers=_auth(other_id),
        )
        assert response.status_code == 200
        assert response.get_json()["requests"] == []

        # And USER_A still sees exactly their own.
        own = self._get(client).get_json()["requests"]
        assert len(own) == 1
        assert own[0]["payment_method_id"] == pm.id

    def test_payload_exposes_safe_fields_only(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        assert _post(client, _valid_cash_payload(pm.id)).status_code == 200

        body = self._get(client).get_json()
        keys = _keys(body)
        for forbidden in (
            "pm_destination",
            "user_destination",
            "created_by",
            "updated_by",
        ):
            assert forbidden not in keys, forbidden
        # Platform payout coordinates can never appear as a VALUE.
        assert PM_DESTINATION not in json.dumps(body)

        row = body["requests"][0]
        assert row["status"] == "pending"
        assert row["request_id"]
        assert Decimal(row["amount"]) == Decimal(CASH_AMOUNT)

    def test_newest_first_and_bounded_limit(self, client, env):
        """Own history in deterministic newest-first order, honoring
        the bounded ``limit`` (transport-validated)."""
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()

        # An older, already-rejected request (outside the cooldown)…
        older_id = "older-request-0001"
        older_quote = RateQuote(
            RATE, "manual",
            datetime.now(timezone.utc) - timedelta(days=1),
        )
        service = WithdrawalService(db_path=env)
        service.create(
            USER_A,
            METHOD_VODAFONE_CASH,
            CASH_AMOUNT,
            payment_method_id=pm.id,
            user_destination=USER_DEST,
            request_id=older_id,
            now=datetime.now(timezone.utc) - timedelta(hours=25),
            quote=older_quote,
        )
        service.reject(older_id)

        # …then a fresh PENDING creation through the API.
        created = _post(
            client, _valid_cash_payload(pm.id)
        ).get_json()["request"]

        body = self._get(client).get_json()["requests"]
        assert [row["request_id"] for row in body] == [
            created["request_id"],
            older_id,
        ]
        assert [row["status"] for row in body] == [
            "pending",
            "rejected",
        ]

        bounded = self._get(client, query="?limit=1").get_json()[
            "requests"
        ]
        assert len(bounded) == 1
        assert bounded[0]["request_id"] == created["request_id"]

    def test_invalid_limit_rejected(self, client, env):
        for bad in ("0", "-1", "abc", "99", "1.5"):
            response = self._get(client, query=f"?limit={bad}")
            assert response.status_code == 400, bad

    def test_read_mutates_nothing(self, client, env):
        pm = _make_pm(env)
        _seed_settings(env)
        _set_rate(env)
        _fund()
        _post(client, _valid_cash_payload(pm.id))

        before = _count_withdrawals(env)
        wallet_before = _wallet_units(env)
        assert self._get(client).status_code == 200
        assert _count_withdrawals(env) == before
        assert _wallet_units(env) == wallet_before
