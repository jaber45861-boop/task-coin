"""
Focused tests — User Deposit Flow Foundation (MT-ADMIN-28)
==========================================================

The deposit domain/backend foundation plus the user-facing deposit
information flow:

    GET  /api/deposit/methods → methods EXPLICITLY configured as
         active deposit methods (safe fields; platform destination
         included on purpose — it is where the user SENDS funds)
    POST /api/deposit         → ONE persisted PENDING deposit intent
         (active deposit method → exact integer minimum → snapshot
         INSERT).  Wallet and ledger are NEVER touched; there is no
         authoritative verification source yet, so nothing is ever
         credited.

Coverage required by MT-ADMIN-28 (38 tests):

A. METHOD DISCOVERY (1–7)
   1  authenticated user receives active deposit methods
   2  inactive methods excluded
   3  withdrawal-only methods excluded
   4  platform deposit destination returned only for explicitly
      active deposit methods
   5  admin-only fields never returned
   6  unauthenticated request rejected
   7  client user_id cannot impersonate another user

B. DEPOSIT REQUEST (8–15)
   8  valid request persists
   9  user_destination is never required/stored for deposits
   10 selected method must be active
   11 invalid method rejected
   12 method facts are snapshotted
   13 platform destination snapshot is preserved
   14 inactive method after creation does not mutate historical request
   15 request is not automatically marked credited

C. AMOUNTS (16–21)
   16 exact integer units
   17 malformed amount rejected
   18 zero/negative rejected
   19 overflow rejected
   20 minimum deposit enforced (and the missing-setting contract)
   21 no float arithmetic

D. WALLET SAFETY (22–25)
   22 creating a deposit request does not credit wallet
   23 creating a deposit request does not write a credit ledger entry
   24 no fake blockchain transaction is created
   25 no fake conversion rate is created

E. IDEMPOTENCY (26–28)
   26 duplicate request behavior is deterministic
   27 request IDs are unique
   28 future verification fields do not permit ambiguous identity

F. AUTH / SECURITY (29–32)
   29 no arbitrary platform destination accepted from client
   30 client cannot choose asset/network/provider independently
   31 no secret/admin fields exposed
   32 no traceback returned

G. UI (33–38)
   33 Deposit loads methods dynamically
   34 destination copy action works
   35 deposit instructions display correctly
   36 inactive methods disappear
   37 no false "credited" success is shown
   38 existing withdrawal UI remains unchanged

Temp databases only; no production destinations or balances are used.

Run:
    python3 -m pytest test_deposit.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from decimal import Decimal

import pytest

import db
import config
import deposit_proof_admin
import deposit_store
import payment_method_store
import serve_miniapp
import wallet
from config import CHANNELS

# Reuse the existing, independently implemented Telegram initData helper.
from test_miniapp_auth import _TEST_BOT_TOKEN, _make_init_data

INIT_DATA_HEADER = "X-Telegram-Init-Data"

USER_A = 4401
IMPERSONATED = 7777
ADMIN_ID = 900_028

PM_DESTINATION = "TEST-PLATFORM-DEP-28"
PM_DESTINATION_2 = "TEST-PLATFORM-DEP-28-B"

# minimum_deposit_units = 1 USDT (exact atomic units).
MIN_UNITS = 100_000_000

_ALLOWED_METHOD_FIELDS = {
    "id",
    "display_name",
    "asset",
    "network",
    "provider",
    "destination",
    "instructions",
}
_FORBIDDEN_ADMIN_KEYS = {
    "created_by",
    "updated_by",
    "sort_order",
    "is_active",
    "deposits_enabled",
    "category",
    "external_tx_id",
    "user_destination",
    "status_label",
}


# ── Fixtures / helpers ────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Environment + isolated database + admin configuration."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TEST_BOT_TOKEN)
    db_path = str(tmp_path / "deposit.db")
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


def _make_pm(db_path: str, *, enabled: bool = True, active: bool = True,
             min_units: int | None = MIN_UNITS,
             **overrides) -> payment_method_store.PaymentMethod:
    kwargs = dict(
        category="crypto",
        display_name="TEST DEPOSIT",
        asset="USDT",
        network="BEP20",
        provider="TEST-PROVIDER",
        destination=PM_DESTINATION,
        instructions="Send exactly the requested amount",
        min_deposit_units=min_units,
        created_by=ADMIN_ID,
        db_path=db_path,
    )
    kwargs.update(overrides)
    method = payment_method_store.create_payment_method(**kwargs)
    if enabled:
        payment_method_store.set_payment_method_deposits_enabled(
            method.id, True, updated_by=ADMIN_ID, db_path=db_path
        )
    if not active:
        payment_method_store.set_payment_method_active(
            method.id, False, updated_by=ADMIN_ID, db_path=db_path
        )
    return payment_method_store.get_payment_method(method.id, db_path)


def _get_methods(client, headers: dict | None = None):
    return client.get(
        "/api/deposit/methods",
        headers=headers if headers is not None else _auth(),
    )


def _post(client, payload: dict, headers: dict | None = None,
          query: str = ""):
    return client.post(
        f"/api/deposit{query}",
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers if headers is not None else _auth(),
    )


def _payload(pm_id: int, amount: str = "1.5", **extra) -> dict:
    body = {"payment_method_id": pm_id, "amount": amount}
    body.update(extra)
    return body


def _raw(db_path: str, sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _deposit_rows(db_path: str) -> list:
    return _raw(db_path, "SELECT * FROM deposit_requests ORDER BY created_at")


def _deposit_columns(db_path: str) -> list:
    return [
        row["name"]
        for row in _raw(db_path, "PRAGMA table_info(deposit_requests)")
    ]


def _wallet_row(db_path: str, user_id: int = USER_A):
    rows = _raw(
        db_path,
        "SELECT available_units, held_units FROM wallets WHERE user_id = ?",
        (user_id,),
    )
    return rows[0] if rows else None


def _ledger_count(db_path: str, user_id: int = USER_A) -> int:
    return _raw(
        db_path, "SELECT COUNT(*) AS n FROM ledger WHERE user_id = ?",
        (user_id,),
    )[0]["n"]


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


def _read_js(name: str) -> str:
    with open(os.path.join("miniapp", "js", name), "r",
              encoding="utf-8") as handle:
        return handle.read()


# ══════════════════════════════════════════════════════════════════
# A. METHOD DISCOVERY (1–7)
# ══════════════════════════════════════════════════════════════════


class TestMethodDiscovery:

    def test_01_authenticated_user_gets_active_deposit_methods(
        self, client, env
    ):
        """1. An authenticated user receives the active deposit
        methods with the safe user-facing fields."""
        pm = _make_pm(env)
        response = _get_methods(client)
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert len(data["methods"]) == 1
        method = data["methods"][0]
        assert set(method) == _ALLOWED_METHOD_FIELDS
        assert method["id"] == pm.id
        assert method["display_name"] == "TEST DEPOSIT"
        assert method["asset"] == "USDT"
        assert method["network"] == "BEP20"
        assert method["provider"] == "TEST-PROVIDER"

    def test_02_inactive_methods_excluded(self, client, env):
        """2. A deposit method that is deactivated disappears."""
        _make_pm(env, active=False)
        response = _get_methods(client)
        assert response.status_code == 200
        assert response.get_json()["methods"] == []

    def test_03_withdrawal_only_methods_excluded(self, client, env):
        """3. An ACTIVE method never opted in as a deposit method
        (withdrawal-only) is not a deposit method."""
        withdrawal_only = _make_pm(env, enabled=False)
        response = _get_methods(client)
        assert response.status_code == 200
        methods = response.get_json()["methods"]
        assert methods == []
        assert withdrawal_only.deposits_enabled is False

    def test_04_destination_only_for_explicit_active_deposit_methods(
        self, client, env
    ):
        """4. The platform deposit destination is reachable ONLY for
        methods that are both active AND deposit-enabled."""
        _make_pm(env, enabled=False)          # active, not a deposit method
        disabled = _make_pm(
            env, enabled=True, active=False, destination=PM_DESTINATION_2
        )
        response = _get_methods(client)
        assert PM_DESTINATION not in response.get_data(as_text=True)
        assert PM_DESTINATION_2 not in response.get_data(as_text=True)
        assert response.get_json()["methods"] == []
        assert disabled.deposits_enabled is True  # inactive → hidden

        # Opting in a method exposes ONLY its own destination.
        pm = _make_pm(env)
        response = _get_methods(client)
        methods = response.get_json()["methods"]
        assert [m["destination"] for m in methods] == [PM_DESTINATION]

    def test_05_admin_only_fields_never_returned(self, client, env):
        """5. Admin/audit metadata and the availability flags are
        never part of a user response."""
        _make_pm(env)
        response = _get_methods(client)
        data = response.get_json()
        assert set(data) == {"ok", "methods"}
        assert set(data["methods"][0]) == _ALLOWED_METHOD_FIELDS
        assert not (_keys(data) & _FORBIDDEN_ADMIN_KEYS)
        assert "created_by" not in response.get_data(as_text=True)
        assert "updated_by" not in response.get_data(as_text=True)

    def test_06_unauthenticated_rejected(self, client, env):
        """6. Both endpoints require verified initData."""
        _make_pm(env)

        response = client.get("/api/deposit/methods")
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

        response = client.post(
            "/api/deposit",
            data=json.dumps(_payload(1)),
            content_type="application/json",
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthenticated"

        # Garbage initData is equally unauthenticated.
        bad = {INIT_DATA_HEADER: "not-valid-init-data"}
        assert _get_methods(client, headers=bad).status_code == 401

    def test_07_client_user_id_cannot_impersonate(self, client, env):
        """7. Identity comes only from verified initData — a body
        user_id is ignored and never becomes the requester."""
        pm = _make_pm(env)

        response = _post(
            client, _payload(pm.id, user_id=IMPERSONATED, user=IMPERSONATED)
        )
        assert response.status_code == 200
        rows = _deposit_rows(env)
        assert len(rows) == 1
        assert rows[0]["user_id"] == USER_A
        assert rows[0]["user_id"] != IMPERSONATED

        # A query-string user_id is equally meaningless.
        response = client.get(
            f"/api/deposit/methods?user_id={IMPERSONATED}",
            headers=_auth(),
        )
        assert response.status_code == 200
        assert response.get_json()["methods"][0]["id"] == pm.id


# ══════════════════════════════════════════════════════════════════
# B. DEPOSIT REQUEST (8–15)
# ══════════════════════════════════════════════════════════════════


class TestDepositRequest:

    def test_08_valid_request_persists(self, client, env):
        """8. A valid request creates exactly one persisted row."""
        pm = _make_pm(env)

        response = _post(client, _payload(pm.id))
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        request = data["request"]
        assert request["status"] == "pending"
        assert request["amount_units"] == 150_000_000

        rows = _deposit_rows(env)
        assert len(rows) == 1
        row = rows[0]
        assert row["request_id"] == request["request_id"]
        assert row["user_id"] == USER_A
        assert row["payment_method_id"] == pm.id
        assert row["amount_units"] == 150_000_000
        assert row["status"] == "pending"
        assert row["created_at"]
        assert row["updated_at"]

    def test_09_user_destination_never_required_or_stored(
        self, client, env
    ):
        """9. Deposits are sent TO the platform — there is no
        user_destination concept: no column, no requirement, and a
        client-supplied value is ignored."""
        pm = _make_pm(env)
        assert "user_destination" not in _deposit_columns(env)

        # Works without any user_destination in the body…
        response = _post(client, _payload(pm.id))
        assert response.status_code == 200

        # …and a client-provided one changes nothing.
        response = _post(
            client,
            _payload(pm.id, user_destination="+20100000000 (FAKE)"),
        )
        assert response.status_code == 200
        rows = _deposit_rows(env)
        assert len(rows) == 2
        for row in rows:
            assert "user_destination" not in dict(row)

    def test_10_selected_method_must_be_active(self, client, env):
        """10. An inactive method can never back a deposit request."""
        pm = _make_pm(env, active=False)

        response = _post(client, _payload(pm.id))
        assert response.status_code == 409
        assert response.get_json()["error"] == "payment_method_unavailable"
        assert _deposit_rows(env) == []

    def test_11_invalid_method_rejected(self, client, env):
        """11. Unknown/malformed method ids are rejected safely."""

        response = _post(client, _payload(999_999))
        assert response.status_code == 404
        assert response.get_json()["error"] == "payment_method_not_found"

        for bad in ("1", None, 0, -3, True, 1.5):
            response = _post(
                client, {"payment_method_id": bad, "amount": "1.5"}
            )
            assert response.status_code == 400
            assert response.get_json()["error"] == "invalid_request"

        assert _deposit_rows(env) == []

        # An active method that is NOT a deposit method also fails.
        pm = _make_pm(env, enabled=False)
        response = _post(client, _payload(pm.id))
        assert response.status_code == 409
        assert (
            response.get_json()["error"] == "deposit_method_unavailable"
        )
        assert _deposit_rows(env) == []

    def test_12_method_facts_snapshotted(self, client, env):
        """12. The user-relevant method facts are copied into the
        request at creation time."""
        pm = _make_pm(env)

        response = _post(client, _payload(pm.id))
        row = _deposit_rows(env)[0]
        assert row["pm_display_name"] == pm.display_name
        assert row["pm_asset"] == pm.asset
        assert row["pm_network"] == pm.network
        assert row["pm_provider"] == pm.provider
        request = response.get_json()["request"]
        assert request["display_name"] == pm.display_name
        assert request["asset"] == pm.asset
        assert request["provider"] == pm.provider

    def test_13_platform_destination_snapshot_preserved(
        self, client, env
    ):
        """13. The PLATFORM deposit destination is snapshotted for
        later audit (it is where the user must send funds)."""
        pm = _make_pm(env)

        _post(client, _payload(pm.id))
        row = _deposit_rows(env)[0]
        assert row["pm_destination"] == PM_DESTINATION
        assert row["pm_destination"] == pm.destination

    def test_14_later_method_change_does_not_mutate_history(
        self, client, env
    ):
        """14. Deactivating/renaming the method afterwards leaves the
        historical request byte-for-byte unchanged."""
        pm = _make_pm(env)
        _post(client, _payload(pm.id))
        before = dict(_deposit_rows(env)[0])

        payment_method_store.set_payment_method_active(
            pm.id, False, updated_by=ADMIN_ID, db_path=env
        )
        payment_method_store.update_payment_method(
            pm.id,
            category=pm.category,
            display_name="RENAMED LATER",
            asset="BTC",
            network="LEGACY",
            provider="OTHER",
            destination="CHANGED-DEST",
            instructions=None,
            updated_by=ADMIN_ID,
            db_path=env,
        )

        after = dict(_deposit_rows(env)[0])
        assert after == before
        assert after["status"] == "pending"

    def test_15_request_never_automaticaly_credited(self, client, env):
        """15. Creating a request NEVER produces a credited/completed
        status — pending/unverified is the only possible outcome."""
        pm = _make_pm(env)

        response = _post(client, _payload(pm.id))
        data = response.get_json()
        assert data["request"]["status"] == "pending"
        assert data["message"]  # pending message, not a credit claim

        row = _deposit_rows(env)[0]
        assert row["status"] == "pending"
        assert row["status"] != "credited"
        assert deposit_store.STATUS_CREDITED not in (
            row["status"],
        )


# ══════════════════════════════════════════════════════════════════
# C. AMOUNTS (16–21)
# ══════════════════════════════════════════════════════════════════


class TestAmounts:

    def test_16_exact_integer_units(self, client, env):
        """16. The amount persists as exact integer atomic units of
        the method's asset (USDT: 8 dp)."""
        pm = _make_pm(env, min_units=1)   # floor of 1 unit — precision

        response = _post(client, _payload(pm.id, "1.5"))
        assert response.status_code == 200
        data = response.get_json()
        assert type(data["request"]["amount_units"]) is int
        assert data["request"]["amount_units"] == 150_000_000
        # Exact decimal TEXT round-trip (never a float).
        assert Decimal(data["request"]["amount"]) == Decimal("1.5")

        # 8-decimal precision stays exact (1 unit = 1e-8 USDT).
        response = _post(client, _payload(pm.id, "0.00000001"))
        assert response.status_code == 200
        assert response.get_json()["request"]["amount_units"] == 1

        rows = _deposit_rows(env)
        assert [r["amount_units"] for r in rows] == [
            150_000_000, 1,
        ]

    def test_17_malformed_amount_rejected(self, client, env):
        """17. Malformed amounts fail deterministically with no row."""
        pm = _make_pm(env)

        for bad in ("abc", "1.2.3", "", "0x10", "1,5", None,
                    [1], {}):
            response = _post(
                client, {"payment_method_id": pm.id, "amount": bad}
            )
            assert response.status_code == 400, bad
            assert response.get_json()["error"] in (
                "invalid_amount", "invalid_request"
            )
        assert _deposit_rows(env) == []

    def test_18_zero_and_negative_rejected(self, client, env):
        """18. Zero and negative amounts are rejected."""
        pm = _make_pm(env)

        for bad in ("0", "0.00000000", "-1", "-0.5", 0):
            response = _post(
                client, {"payment_method_id": pm.id, "amount": bad}
            )
            assert response.status_code == 400, bad
            assert response.get_json()["error"] == "invalid_amount"
        assert _deposit_rows(env) == []

    def test_19_overflow_rejected(self, client, env):
        """19. Amounts beyond the signed SQLite INTEGER bound are
        rejected — never wrapped, never truncated."""
        pm = _make_pm(env)

        huge = str(2 ** 63)          # 9_223_372_036_854_775_808
        for bad in (huge, "99999999999999999999",
                    str(10 ** 30)):
            response = _post(
                client, {"payment_method_id": pm.id, "amount": bad}
            )
            assert response.status_code == 400
            assert response.get_json()["error"] == "invalid_amount"
        assert _deposit_rows(env) == []

    def test_20_minimum_deposit_enforced(self, client, env):
        """20. The PER-METHOD minimum (min_deposit_units, atomic
        units of the method's own asset) is enforced exactly; a
        missing value never falls back to any default."""
        pm = _make_pm(env)   # min_deposit_units = MIN_UNITS (USDT)

        response = _post(client, _payload(pm.id, "0.5"))
        assert response.status_code == 400
        assert response.get_json()["error"] == "below_minimum"
        assert _deposit_rows(env) == []

        # Exactly the minimum is accepted.
        response = _post(client, _payload(pm.id, "1"))
        assert response.status_code == 200
        assert response.get_json()["request"]["amount_units"] == MIN_UNITS

    def test_20b_missing_minimum_setting_is_not_invented(
        self, client, env
    ):
        """20b. A method whose minimum is not configured stays
        fail-closed: 503 deposit_settings_missing, no default is
        invented.  Production-reachable path: changing the asset
        clears the stored minimum while the method stays published
        (old units would belong to the old asset's scale)."""
        pm = _make_pm(env)
        payment_method_store.update_payment_method(
            pm.id,
            category=pm.category,
            display_name=pm.display_name,
            asset="EGP",
            network=pm.network,
            provider=pm.provider,
            destination=pm.destination,
            instructions=pm.instructions,
            updated_by=ADMIN_ID,
            db_path=env,
        )
        updated = payment_method_store.get_payment_method(pm.id, env)
        assert updated.min_deposit_units is None   # cleared, not kept
        assert updated.deposits_enabled is True    # still published

        response = _post(client, _payload(pm.id, "10"))
        assert response.status_code == 503
        assert response.get_json()["error"] == "deposit_settings_missing"
        assert _deposit_rows(env) == []

    def test_21_no_float_arithmetic(self, client, env):
        """21. JSON float amounts are rejected at the transport edge;
        stored values are exact ints — no float ever reaches storage."""
        pm = _make_pm(env)

        response = _post(
            client,
            {"payment_method_id": pm.id, "amount": 1.5},
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_amount"
        assert _deposit_rows(env) == []

        response = _post(client, _payload(pm.id, "2.05"))
        assert response.status_code == 200
        units = response.get_json()["request"]["amount_units"]
        assert type(units) is int
        assert units == 205_000_000
        stored = _deposit_rows(env)[0]["amount_units"]
        assert type(stored) is int
        assert isinstance(stored, int) and not isinstance(stored, float)


# ══════════════════════════════════════════════════════════════════
# D. WALLET SAFETY (22–25)
# ══════════════════════════════════════════════════════════════════


class TestWalletSafety:

    def test_22_creating_request_does_not_credit_wallet(
        self, client, env
    ):
        """22. No wallet credit — balances are byte-for-byte
        unchanged, and no wallet row is even created."""
        pm = _make_pm(env)

        # A funded user's balance stays identical…
        wallet.ensure_wallet(USER_A)
        wallet.credit_units(USER_A, 1_000_000_000)
        before = _wallet_row(env, USER_A)
        _post(client, _payload(pm.id))
        assert _wallet_row(env, USER_A) == before

        # …and an unfunded requester never gets a wallet row.
        _post(client, _payload(pm.id), headers=_auth(IMPERSONATED))
        assert _wallet_row(env, IMPERSONATED) is None

    def test_23_no_credit_ledger_entry(self, client, env):
        """23. No ledger row of any kind is written for a deposit
        intent (nothing references the request)."""
        pm = _make_pm(env)
        before = _ledger_count(env)

        response = _post(client, _payload(pm.id))
        request_id = response.get_json()["request"]["request_id"]

        assert _ledger_count(env) == before
        assert _raw(
            env,
            "SELECT * FROM ledger WHERE reference_type = 'deposit' "
            "OR reference_id = ?",
            (request_id,),
        ) == []

    def test_24_no_fake_blockchain_transaction(self, client, env):
        """24. No transaction id is fabricated: external_tx_id stays
        NULL and no tx-shaped field is invented."""
        pm = _make_pm(env)

        response = _post(client, _payload(pm.id))
        row = _deposit_rows(env)[0]
        assert row["external_tx_id"] is None
        assert response.get_json()["request"].get("external_tx_id") is None

        payload = response.get_json()["request"]
        assert not any(
            key in payload
            for key in ("tx_hash", "transaction_hash", "tx_id",
                        "external_tx_id", "confirmations")
        )
        assert "external_tx_id" in _deposit_columns(env)  # future key,
        # present but deliberately NULL — never invented at creation.

    def test_25_no_fake_conversion_rate(self, client, env):
        """25. No rate/conversion fact exists anywhere in the deposit
        flow — crediting a non-USDT asset later requires an explicit
        future mapping rule."""
        pm = _make_pm(env)

        columns = _deposit_columns(env)
        assert not any("rate" in name for name in columns)
        assert not any("conversion" in name for name in columns)

        response = _post(client, _payload(pm.id))
        data = response.get_json()
        assert not any("rate" in key for key in _keys(data))
        # The wallet was never consulted for a conversion either.
        assert _wallet_row(env) is None


# ══════════════════════════════════════════════════════════════════
# E. IDEMPOTENCY (26–28)
# ══════════════════════════════════════════════════════════════════


class TestIdempotency:

    def test_26_duplicate_request_behavior_deterministic(
        self, client, env
    ):
        """26. The same payload twice behaves identically every time:
        two independent PENDING intents with distinct ids (a deposit
        intent reserves nothing, so repeats are allowed by design)."""
        pm = _make_pm(env)

        first = _post(client, _payload(pm.id, "2"))
        second = _post(client, _payload(pm.id, "2"))
        assert first.status_code == second.status_code == 200
        a = first.get_json()["request"]
        b = second.get_json()["request"]
        assert a["request_id"] != b["request_id"]
        assert a["status"] == b["status"] == "pending"
        assert a["amount_units"] == b["amount_units"] == 200_000_000

        rows = _deposit_rows(env)
        assert len(rows) == 2
        assert all(row["status"] == "pending" for row in rows)

    def test_27_request_ids_unique(self, client, env):
        """27. Request ids are unique across every request (PK)."""
        pm = _make_pm(env)

        ids = set()
        for _ in range(5):
            response = _post(client, _payload(pm.id, "1"))
            ids.add(response.get_json()["request"]["request_id"])
        assert len(ids) == 5

        with pytest.raises(sqlite3.IntegrityError):
            conn = sqlite3.connect(env)
            try:
                duplicate = _deposit_rows(env)[0]["request_id"]
                conn.execute(
                    "INSERT INTO deposit_requests "
                    "(request_id, user_id, payment_method_id, "
                    " amount_units, status, pm_display_name, pm_asset, "
                    " pm_network, pm_provider, pm_destination) "
                    "VALUES (?, ?, ?, ?, 'pending', 'x', 'USDT', NULL, "
                    " 'p', 'd')",
                    (duplicate, USER_A, pm.id, 1),
                )
                conn.commit()
            finally:
                conn.close()

    def test_28_future_verification_identity_unambiguous(
        self, client, env
    ):
        """28. The future verification key cannot become ambiguous:
        external_tx_id is NULL at creation and a partial UNIQUE index
        guarantees one external transaction maps to at most one
        request — the precondition for an idempotent credit."""
        pm = _make_pm(env)
        _post(client, _payload(pm.id))

        index = _raw(
            env,
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'ux_deposit_requests_tx'",
        )
        assert len(index) == 1
        assert "UNIQUE" in index[0]["sql"].upper()

        request_id = _deposit_rows(env)[0]["request_id"]

        def _insert(tx_id: str) -> None:
            conn = sqlite3.connect(env)
            try:
                conn.execute(
                    "INSERT INTO deposit_requests "
                    "(request_id, user_id, payment_method_id, "
                    " amount_units, status, pm_display_name, pm_asset, "
                    " pm_network, pm_provider, pm_destination, "
                    " external_tx_id) "
                    "VALUES (?, ?, ?, ?, 'pending', 'x', 'USDT', NULL, "
                    " 'p', 'd', ?)",
                    (f"other-{tx_id}", USER_A, pm.id, 1, tx_id),
                )
                conn.commit()
            finally:
                conn.close()

        _insert("chain-tx-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert("chain-tx-1")   # same external tx → second credit impossible
        # NULL rows (unverified intents) are NOT part of the index.
        assert _deposit_rows(env)[0]["request_id"] == request_id


# ══════════════════════════════════════════════════════════════════
# F. AUTH / SECURITY (29–32)
# ══════════════════════════════════════════════════════════════════


class TestSecurity:

    def test_29_no_arbitrary_destination_from_client(
        self, client, env
    ):
        """29. A client-supplied destination can never become the
        stored deposit destination — it always comes from the server's
        configured method row."""
        pm = _make_pm(env)

        response = _post(
            client,
            _payload(
                pm.id,
                destination="ATTACKER-DEST",
                pm_destination="ATTACKER-DEST-2",
            ),
        )
        assert response.status_code == 200
        row = _deposit_rows(env)[0]
        assert row["pm_destination"] == PM_DESTINATION
        assert "ATTACKER-DEST" not in json.dumps(response.get_json())

    def test_30_client_cannot_choose_asset_network_provider(
        self, client, env
    ):
        """30. asset/network/provider resolve server-side from the
        payment method — client values are ignored entirely."""
        pm = _make_pm(env)

        response = _post(
            client,
            _payload(
                pm.id,
                asset="BTC",
                network="ATTACKER-NET",
                provider="ATTACKER-PROVIDER",
            ),
        )
        assert response.status_code == 200
        request = response.get_json()["request"]
        assert request["asset"] == "USDT"
        assert request["network"] == "BEP20"
        assert request["provider"] == "TEST-PROVIDER"

        row = _deposit_rows(env)[0]
        assert row["pm_asset"] == "USDT"
        assert row["pm_network"] == "BEP20"
        assert row["pm_provider"] == "TEST-PROVIDER"
        assert "ATTACKER" not in json.dumps(response.get_json())

    def test_31_no_secret_or_admin_fields_exposed(self, client, env):
        """31. Neither endpoint ever emits admin/audit columns or
        availability internals."""
        _make_pm(env)

        methods = _get_methods(client).get_json()
        created = _post(
            client, _payload(methods["methods"][0]["id"])
        ).get_json()

        for data in (methods, created):
            assert not (_keys(data) & _FORBIDDEN_ADMIN_KEYS)
        text = (
            _get_methods(client).get_data(as_text=True)
            + json.dumps(created)
        )
        for forbidden in ("created_by", "updated_by", "sort_order",
                          "sqlite", "Traceback"):
            assert forbidden not in text

    def test_32_no_traceback_returned(self, client, env):
        """32. Malformed transport and unexpected server failures
        both yield concise Arabic errors — never a traceback or
        internal detail."""
        pm = _make_pm(env)

        response = client.post(
            "/api/deposit",
            data="{not json",
            content_type="application/json",
            headers=_auth(),
        )
        assert response.status_code == 400
        text = response.get_data(as_text=True)
        assert "Traceback" not in text
        assert "sqlite" not in text

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                deposit_store,
                "create_deposit_request",
                lambda **kwargs: (_ for _ in ()).throw(
                    RuntimeError("SECRET_SQL_DETAIL exploded")
                ),
            )
            response = _post(client, _payload(pm.id))
        assert response.status_code == 500
        data = response.get_json()
        assert data["error"] == "server_error"
        text = response.get_data(as_text=True)
        assert "SECRET_SQL_DETAIL" not in text
        assert "Traceback" not in text
        assert "RuntimeError" not in text


# ══════════════════════════════════════════════════════════════════
# G. UI (33–38)
# ══════════════════════════════════════════════════════════════════


class TestUI:

    def test_33_deposit_loads_methods_dynamically(self):
        """33. The deposit panel fetches methods from the API on
        every open (no hardcoded list) and the Wallet button only
        opens the panel."""
        deposit = _read_js("deposit.js")
        wallet_js = _read_js("wallet.js")

        assert "const METHODS_URL = '/api/deposit/methods'" in deposit
        assert "await fetch(METHODS_URL" in deposit
        assert "Array.isArray(data.methods)" in deposit
        # Wallet is delegation-only: no fetch, no method list.
        assert "DepositUI.open()" in wallet_js
        assert "fetch(" not in wallet_js

        with open("miniapp/index.html", encoding="utf-8") as handle:
            html = handle.read()
        assert 'js/deposit.js' in html

    def test_34_destination_copy_action_works(self):
        """34. The platform destination has a working copy action
        (clipboard write on the configured destination only)."""
        deposit = _read_js("deposit.js")
        assert 'data-testid="deposit-copy"' in deposit
        assert "navigator.clipboard.writeText(method.destination)" in deposit
        assert "addEventListener('click'" in deposit
        assert "'✅ تم النسخ'" in deposit
        # The destination is rendered from server data, escaped.
        assert 'value="${_esc(method.destination)}"' in deposit

    def test_35_deposit_instructions_display_correctly(self):
        """35. Server-provided instructions render escaped, from the
        method object (never client-invented text)."""
        deposit = _read_js("deposit.js")
        assert 'data-testid="deposit-instructions"' in deposit
        assert "_esc(method.instructions)" in deposit
        assert "method.instructions" in deposit

    def test_36_inactive_methods_disappear(self):
        """36. The panel refetches on every open and drops its cache
        on close — a method deactivated after the last open can never
        be offered again."""
        deposit = _read_js("deposit.js")
        assert "methodsCache = null" in deposit            # close() clears
        open_body = deposit[
            deposit.index("async function open()"):
            deposit.index("function close()")
        ]
        assert "await fetch(METHODS_URL" in open_body       # fresh fetch
        # Backend side: inactive/withdrawal-only methods are excluded.
        source = open("deposit_routes.py", encoding="utf-8").read()
        assert "active_only=True" in source
        assert "deposits_enabled" in source

    def test_37_no_false_credited_success_shown(self):
        """37. The created-request view only ever shows the pending
        (unverified) state — it never claims funds arrived."""
        deposit = _read_js("deposit.js")
        assert "⏳ قيد التحقق" in deposit
        assert 'data-testid="deposit-status"' in deposit
        assert "بانتظار التحقق" in deposit
        for claim in (
            "تمت إضافة", "أُضيف", "تم الإيداع بنجاح",
            "credited", "تمت إضافة رصيدك",
        ):
            assert claim not in deposit, claim
        # "مكتمل" may only appear inside the settings-incomplete
        # error — never as a status label for a created request.
        status_block = deposit[
            deposit.index("STATUS_LABELS"):
            deposit.index("let overlay")
        ]
        assert "مكتمل" not in status_block
        assert "credited" not in status_block
        # No credited status label exists in this phase.
        assert "credited:" not in deposit

        # Server message itself promises verification, not credit.
        routes = open("deposit_routes.py", encoding="utf-8").read()
        assert "بانتظار التحقق" in routes

    def test_38_existing_withdrawal_ui_unchanged(self):
        """38. The withdrawal UI is untouched by this task: the file
        is byte-identical to git HEAD and the existing wiring is
        still intact."""
        result = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--",
             "miniapp/js/withdrawal.js"],
            capture_output=True,
        )
        assert result.returncode == 0, "withdrawal.js was modified"

        withdrawal = _read_js("withdrawal.js")
        assert "const METHODS_URL = '/api/withdrawal/methods'" in withdrawal
        assert "const CREATE_URL = '/api/withdrawal'" in withdrawal

        wallet_js = _read_js("wallet.js")
        assert "WithdrawalUI.open()" in wallet_js
        assert "fetch(" not in wallet_js

        with open("miniapp/index.html", encoding="utf-8") as handle:
            html = handle.read()
        assert 'js/withdrawal.js' in html


# ══════════════════════════════════════════════════════════════════
# H. PER-ASSET MINIMUM / SCALE (39–47)
# ══════════════════════════════════════════════════════════════════


class TestPerAssetMinimums:
    """HTTP-level proof that minimums and amounts follow the method's
    OWN asset scale (EGP 2 dp vs USDT 8 dp) — one global scale or a
    cross-currency comparison is impossible by construction."""

    @staticmethod
    def _make_egp_pm(db_path: str, min_units: int = 5000):
        return _make_pm(
            db_path,
            category="cash",
            asset="EGP",
            network=None,
            display_name="TEST EGP",
            min_units=min_units,
        )

    def test_39_egp_below_minimum_rejected(self, client, env):
        """39. EGP 49.99 < 50 EGP → below_minimum, no row."""
        pm = self._make_egp_pm(env)
        response = _post(client, _payload(pm.id, "49.99"))
        assert response.status_code == 400
        assert response.get_json()["error"] == "below_minimum"
        assert _deposit_rows(env) == []

    def test_40_egp_50_accepted_with_exact_egp_units(self, client, env):
        """40. EGP 50 → accepted, stored as 5000 EGP cents — NEVER
        the old 8-dp USDT interpretation (5_000_000_000)."""
        pm = self._make_egp_pm(env)
        response = _post(client, _payload(pm.id, "50"))
        assert response.status_code == 200
        request = response.get_json()["request"]
        assert request["amount_units"] == 5000
        assert request["amount_units"] != 5_000_000_000
        assert request["amount"] == "50.00"        # 2-dp EGP display
        assert request["asset"] == "EGP"
        assert _deposit_rows(env)[0]["amount_units"] == 5000

    def test_41_egp_over_precision_invalid(self, client, env):
        """41. EGP 0.001 (3 dp) is invalid_amount — precision follows
        the asset scale and is rejected, never rounded."""
        pm = self._make_egp_pm(env)
        response = _post(client, _payload(pm.id, "0.001"))
        assert response.status_code == 400
        assert response.get_json()["error"] == "invalid_amount"
        assert _deposit_rows(env) == []

    def test_42_usdt_below_and_at_minimum(self, client, env):
        """42. USDT 0.99 < 1 USDT → rejected; USDT 1 → accepted as
        exactly 100_000_000 units."""
        pm = _make_pm(env)          # min_deposit_units = 1 USDT
        response = _post(client, _payload(pm.id, "0.99"))
        assert response.status_code == 400
        assert response.get_json()["error"] == "below_minimum"
        assert _deposit_rows(env) == []

        response = _post(client, _payload(pm.id, "1"))
        assert response.status_code == 200
        assert response.get_json()["request"]["amount_units"] == 100_000_000

    def test_43_usdt_precision_not_limited_by_egp_scale(self, client, env):
        """43. USDT keeps full 8-dp precision (0.001 and 1.00000001
        are valid) — the EGP 2-dp scale never leaks into it."""
        pm = _make_pm(env, min_units=1)
        for text, units in (
            ("0.001", 100_000),
            ("1.00000001", 100_000_001),
        ):
            response = _post(client, _payload(pm.id, text))
            assert response.status_code == 200, text
            assert response.get_json()["request"]["amount_units"] == units

    def test_44_same_text_parses_per_own_asset(self, client, env):
        """44. The identical text \"1\" becomes 100 units for EGP and
        100_000_000 for USDT — scale is per-method, never global."""
        egp = self._make_egp_pm(env, min_units=100)
        usdt = _make_pm(env)
        egp_units = _post(
            client, _payload(egp.id, "1")
        ).get_json()["request"]["amount_units"]
        usdt_units = _post(
            client, _payload(usdt.id, "1")
        ).get_json()["request"]["amount_units"]
        assert egp_units == 100
        assert usdt_units == 100_000_000

    def test_45_unknown_asset_fails_closed(self, client, env):
        """45. A published method whose asset has no registered scale
        can never create a request: 503, no default scale, no row."""
        pm = _make_pm(env)
        payment_method_store.update_payment_method(
            pm.id,
            category=pm.category,
            display_name=pm.display_name,
            asset="TESTCOIN",
            network=pm.network,
            provider=pm.provider,
            destination=pm.destination,
            instructions=pm.instructions,
            updated_by=ADMIN_ID,
            db_path=env,
        )
        response = _post(client, _payload(pm.id, "10"))
        assert response.status_code == 503
        assert response.get_json()["error"] == "deposit_settings_missing"
        assert _deposit_rows(env) == []

    def test_46_receipt_and_admin_display_use_asset_scale(
        self, client, env
    ):
        """46. The Mini App receipt renders the request's own asset
        (no hardcoded unit) and the admin proof display renders at
        the asset's registered scale."""
        deposit = _read_js("deposit.js")
        assert (
            "${_esc(request.amount)} ${_esc(request.asset)}" in deposit
        )
        assert ") USDT" not in deposit    # the old hardcoded unit

        egp = self._make_egp_pm(env)
        egp_request_id = _post(
            client, _payload(egp.id, "50")
        ).get_json()["request"]["request_id"]
        egp_row = deposit_store.get_deposit_request(
            egp_request_id, db_path=env
        )
        assert deposit_proof_admin._amount_text(egp_row) == "50.00 EGP"

    def test_47_existing_usdt_request_still_readable(
        self, client, env
    ):
        """47. USDT requests keep their exact historical rendering:
        payload amount at 8 dp and the proof-admin display intact."""
        pm = _make_pm(env)
        response = _post(client, _payload(pm.id, "1.5"))
        assert response.get_json()["request"]["amount"] == "1.50000000"
        request_id = response.get_json()["request"]["request_id"]
        request = deposit_store.get_deposit_request(request_id, db_path=env)
        row = _deposit_rows(env)[0]
        assert row["amount_units"] == 150_000_000
        assert deposit_proof_admin._amount_text(request) == (
            "1.50000000 USDT"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
