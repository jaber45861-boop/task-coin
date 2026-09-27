"""
Focused tests — Atomic Withdrawal Service (MT-ADMIN-23)
========================================================

The production orchestration layer only: create / reject / complete
each run inside ONE ``db.transaction()`` and move money exclusively
through the MT-ADMIN-18..22 boundaries.  No Mini App endpoints,
Telegram handlers, admin UI or deposits exist here.

  CREATE
   1. Vodafone successful create (every schema field pinned)
   2. USDT successful create
   3. exact wallet_debit_units
   4. Vodafone ceiling conversion
   5. USDT direct sum
   6. no USDT -> EGP -> USDT round trip
   7. rate snapshot (immutable after creation)
   8. payment-method snapshot
   9. user_destination persistence (never the platform destination)
  10. transaction rollback leaves no state
  11. duplicate pending -> PendingWithdrawalExistsError
  12. cooldown (24 h, rejected requests still count)
  13. insufficient wallet balance
  14. invalid amount
  15. inactive payment method
  16. missing payment method
  17. no orphan ledger entry
  18. no orphan wallet hold

  REJECT
  19. pending -> rejected
  20. wallet release exact
  21. ledger release exact
  22. rejected_at set
  23. double reject blocked
  24. completed cannot reject
  25. legacy NULL debit blocked safely

  COMPLETE
  26. pending -> completed
  27. wallet settlement exact (held only, no second deduction)
  28. ledger settlement exact
  29. completed_at set
  30. double complete blocked
  31. rejected cannot complete
  32. legacy NULL debit blocked safely

  RACE / ATOMICITY
  33. concurrent duplicate create -> one pending request only
  34. reject/complete race -> exactly one succeeds
  35. every dependency receives the SAME transaction connection
  36. rollback after wallet mutation
  37. rollback after ledger mutation
  38. unexpected exceptions propagate unchanged
  39. idempotency keys are deterministic
  40. no double money movement

  FINANCIAL
  41. sub-cent USDT amounts stay exact
  42. integer-only accounting
  43. no float / no REAL
  44. INT64 boundary behavior

  ERROR BOUNDARY (part L extras)
  - unknown request -> RequestNotFoundError (both flows)
  - insufficient held -> domain error + full rollback
  - unknown user / missing quote -> deterministic domain errors
  - duplicate request_id reuse -> DuplicateRequestError + rollback
  - no hidden connection while a flow is active (part M)

Temp databases only; no production destinations or balances are used.
Money math is Decimal/int only — floats are forbidden.

Run:
    python -m pytest test_withdrawal_service.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import config
import db
import payment_method_store
import platform_settings
import wallet
import withdrawal_rules
import withdrawal_service
from rate_quote import RateQuote
from withdrawal_rules import (
    METHOD_USDT_BEP20,
    METHOD_VODAFONE_CASH,
    CooldownError,
    DuplicateRequestError,
    InsufficientBalanceError,
    InsufficientHeldBalanceError,
    InvalidAmountError,
    InvalidMethodError,
    InvalidStateError,
    MissingRateError,
    PendingWithdrawalExistsError,
    PaymentMethodUnavailableError,
    RequestNotFoundError,
    RequestStatus,
    ValidationError,
    WithdrawalError,
)
from withdrawal_service import MissingWalletDebitError, WithdrawalService
from withdrawal_store import (
    SqliteLedgerAdapter,
    SqliteWalletAdapter,
    SqliteWithdrawalRepository,
)

# ── Exact reference values ──────────────────────────────────────────
# rate = 48.5 EGP per USDT (canonical text "48.5"):
#   vodafone 10.00 + 1.00 fee = 11 EGP
#     11 / 48.5 = 0.226804123711340... -> CEILING at 8 dp
#     -> 0.22680413 USDT = 22_680_413 atomic units
#   usdt fee   1 / 48.5 = 0.02061855670...  -> ceil -> 0.02061856
#   usdt min  10 / 48.5 = 0.20618556701...  -> ceil -> 0.20618557
RATE = Decimal("48.5")
RATE_TEXT = "48.5"
CAPTURED_AT = datetime(2026, 9, 27, 9, 59, 0, tzinfo=timezone.utc)
CAPTURED_TEXT = "2026-09-27T09:59:00+00:00"
QUOTE = RateQuote(RATE, "manual", CAPTURED_AT)
QUOTE_CEIL3 = RateQuote(Decimal("3"), "manual", CAPTURED_AT)   # "3"
QUOTE_3000 = RateQuote(Decimal("3000"), "manual", CAPTURED_AT)  # "3000"

NOW = datetime(2026, 9, 27, 10, 0, 0)          # naive UTC wall-clock
FUND = 1_000_000_000                            # 10 USDT in units
VODAFONE_DEBIT = 22_680_413                     # 11 EGP ceiling @ 48.5
USDT_AMOUNT_1_5 = 150_000_000                   # 1.5 USDT
USDT_FEE_485 = 2_061_856                        # ceil(1/48.5) 8 dp
USDT_DEBIT_1_5 = 152_061_856                    # 150_000_000 + fee
# rate 3000: min = ceil(10/3000) = 0.00333334 -> 333_334 units
#            fee = ceil(1/3000)  = 0.00033334 ->  33_334 units
SUBCENT_AMOUNT = Decimal("0.00333334")
SUBCENT_AMOUNT_UNITS = 333_334
SUBCENT_FEE_UNITS = 33_334
SUBCENT_DEBIT = 366_668

# Fixture admin for the MT-ADMIN-24 platform-settings writes (saved /
# restored around every test, mirroring test_platform_settings).
ADMIN_ID = 900_240

USER_DEST = "+201001234567 (TEST)"
PM_DESTINATION = "TEST-PLATFORM-DEST-9"
USDT_SCALE = 100_000_000


def _make_quote(rate) -> RateQuote:
    return RateQuote(rate, "manual", CAPTURED_AT)


def _rate_derived_settings(rate) -> tuple[int, int]:
    """MT-ADMIN-24 settings that match the approved EGP contract at
    ``rate``: the 10 EGP minimum and 1 EGP fee expressed in exact USDT
    atomic units.  The fixture configures these values so every
    MT-ADMIN-23 expectation (amounts, fees, debits) stays byte-exact
    while the service genuinely reads them from platform_settings.
    """
    minimum_units = wallet.decimal_to_units(
        withdrawal_rules.min_native_for(METHOD_USDT_BEP20, rate),
        field="minimum_withdrawal_units",
    )
    fee_units = wallet.decimal_to_units(
        withdrawal_rules.fee_native_for(METHOD_USDT_BEP20, rate),
        field="withdrawal_fee_units",
    )
    return minimum_units, fee_units


class _FailingRepository(SqliteWithdrawalRepository):
    """Repository whose insert always fails (rollback injection)."""

    def __init__(self, db_path, exc):
        super().__init__(db_path)
        self._exc = exc

    def insert(self, request, *, connection=None):
        raise self._exc


class _FailingLedger(SqliteLedgerAdapter):
    """Ledger whose hold always fails AFTER the wallet reserve ran."""

    def __init__(self, exc):
        self._exc = exc

    def hold(self, user_id, amount_units, *, request_id, connection,
             rate_usdt_egp=None):
        raise self._exc


class _Base(unittest.TestCase):
    """Temp DB fixture + seed/assert helpers."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self._original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db(self.db_path)
        self.svc = WithdrawalService(db_path=self.db_path)
        self.pm = self.create_method()
        self._orig_admins = list(config.ADMINS)
        config.ADMINS[:] = [ADMIN_ID]
        self.addCleanup(self._restore_admins)
        self.seed_withdrawal_settings(*_rate_derived_settings(RATE))
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_PATH = self._original_db_path
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _restore_admins(self):
        config.ADMINS[:] = self._orig_admins

    # ── seed helpers ───────────────────────────────────────────

    def add_user(self, user_id: int = 501) -> int:
        self.assertTrue(
            db.register_user(user_id, f"u{user_id}", f"U{user_id}")
        )
        return user_id

    def fund(self, user_id: int = 501, units: int = FUND) -> int:
        wallet.ensure_wallet(user_id)          # standalone scope
        return wallet.credit_units(user_id, units)

    def create_method(self, *, active: bool = True, **overrides):
        kwargs = dict(
            category="cash",
            display_name="TEST-METHOD",
            asset="EGP",
            provider="TEST-PROVIDER",
            destination=PM_DESTINATION,
            created_by=1,
            db_path=self.db_path,
        )
        kwargs.update(overrides)
        method = payment_method_store.create_payment_method(**kwargs)
        if not active:
            payment_method_store.set_payment_method_active(
                method.id, False, updated_by=1, db_path=self.db_path
            )
        return method

    def seed_withdrawal_settings(self, minimum_units: int,
                                 fee_units: int) -> None:
        """Configure minimum_withdrawal_units / withdrawal_fee_units
        through the production platform-settings contract.

        Runs on a caller-owned raw connection — deliberately NOT via
        ``db.transaction()`` / ``db.get_connection()`` because some
        tests spy on those while a create is in flight.  Values that
        already match are left untouched, so a concurrent create only
        ever reads.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            for key, value in (
                (platform_settings.MINIMUM_WITHDRAWAL_UNITS,
                 minimum_units),
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

    def create_request(self, method=METHOD_VODAFONE_CASH, *,
                       user_id=501, now=NOW, quote=QUOTE, amount=None,
                       svc=None, **extra):
        if amount is None:
            amount = (
                Decimal("10.00")
                if method == METHOD_VODAFONE_CASH
                else Decimal("1.5")
            )
        # MT-ADMIN-24: withdrawals read their minimum/fee from
        # platform_settings — configure the rate-matching values for
        # this quote before every create.
        rate = (
            quote.rate_usdt_egp
            if isinstance(quote, RateQuote)
            else RATE
        )
        self.seed_withdrawal_settings(*_rate_derived_settings(rate))
        kwargs = dict(
            payment_method_id=self.pm.id,
            user_destination=USER_DEST,
            now=now,
            quote=quote,
        )
        kwargs.update(extra)
        service = self.svc if svc is None else svc
        return service.create(user_id, method, amount, **kwargs)

    def insert_legacy_null_debit(self, request_id="wd-legacy",
                                 user_id=501):
        """Pre-MT-ADMIN-19 row: NULL debit, NULL user_destination."""
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor,
                       fee_native_minor, native_unit, rate_usdt_egp,
                       wallet_rate_usdt_egp, rate_captured_at,
                       rate_provider, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, "vodafone_cash", 1000, 100,
                 1000, 100, "EGP", None, RATE_TEXT, CAPTURED_TEXT,
                 "manual", "pending", "2026-01-01 00:00:00"),
            )

    def insert_legacy_big_debit(self, request_id="wd-big", user_id=501,
                                debit=999_999_999):
        """Legacy-shaped pending row whose debit exceeds every hold."""
        with db.get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO withdrawal_requests (
                       request_id, user_id, method, amount_egp_minor,
                       fee_egp_minor, amount_native_minor,
                       fee_native_minor, native_unit, rate_usdt_egp,
                       wallet_rate_usdt_egp, rate_captured_at,
                       rate_provider, status, created_at,
                       wallet_debit_units
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, user_id, "vodafone_cash", 1000, 100,
                 1000, 100, "EGP", None, RATE_TEXT, CAPTURED_TEXT,
                 "manual", "pending", "2026-01-01 00:00:00", debit),
            )

    # ── raw state readers ──────────────────────────────────────

    def raw_row(self, request_id: str) -> sqlite3.Row:
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM withdrawal_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        self.assertIsNotNone(row, f"row {request_id!r} missing")
        return row

    def wallet_state(self, user_id: int = 501) -> tuple:
        with db.get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT available_units, held_units FROM wallets "
                "WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return (row["available_units"], row["held_units"])

    def ledger_rows(self, request_id: str | None = None) -> list:
        sql = "SELECT * FROM ledger"
        params: tuple = ()
        if request_id is not None:
            sql += (
                " WHERE reference_type = 'withdrawal' "
                "AND reference_id = ?"
            )
            params = (request_id,)
        sql += " ORDER BY id"
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchall()

    def request_count(self, user_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM withdrawal_requests"
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE user_id = ?"
            params = (user_id,)
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def pending_count(self, user_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) FROM withdrawal_requests WHERE status='pending'"
        params: tuple = ()
        if user_id is not None:
            sql += " AND user_id = ?"
            params = (user_id,)
        with db.get_connection(self.db_path) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def column_types(self) -> list:
        with db.get_connection(self.db_path) as conn:
            return conn.execute(
                "PRAGMA table_info(withdrawal_requests)"
            ).fetchall()


# ── 1–18: create ────────────────────────────────────────────────────


class TestCreate(_Base):
    def test_01_vodafone_create_success(self):
        """1. Vodafone create persists every schema field exactly."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request()

        self.assertEqual(req.status, RequestStatus.PENDING)
        self.assertEqual(req.request_id, req.request_id)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["user_id"], 501)
        self.assertEqual(row["method"], "vodafone_cash")
        self.assertEqual(row["amount_egp_minor"], 1000)
        self.assertEqual(row["fee_egp_minor"], 100)
        self.assertEqual(row["amount_native_minor"], 1000)
        self.assertEqual(row["fee_native_minor"], 100)
        self.assertEqual(row["native_unit"], "EGP")
        self.assertIsNone(row["rate_usdt_egp"])   # None for Vodafone
        self.assertEqual(row["wallet_rate_usdt_egp"], RATE_TEXT)
        self.assertEqual(row["rate_captured_at"], CAPTURED_TEXT)
        self.assertEqual(row["rate_provider"], "manual")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["created_at"], "2026-09-27 10:00:00")
        self.assertEqual(row["payment_method_id"], self.pm.id)
        self.assertEqual(row["pm_display_name"], "TEST-METHOD")
        self.assertEqual(row["pm_category"], "cash")
        self.assertEqual(row["pm_asset"], "EGP")
        self.assertIsNone(row["pm_network"])
        self.assertEqual(row["pm_provider"], "TEST-PROVIDER")
        self.assertEqual(row["pm_destination"], PM_DESTINATION)
        self.assertEqual(row["wallet_debit_units"], VODAFONE_DEBIT)
        self.assertEqual(row["user_destination"], USER_DEST)
        self.assertIsNone(row["rejected_at"])
        self.assertIsNone(row["completed_at"])
        # wallet moved the exact debit
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT),
        )
        # one ledger hold, canonical reference
        rows = self.ledger_rows(req.request_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_type"], "hold")
        self.assertEqual(rows[0]["amount_units"], VODAFONE_DEBIT)
        self.assertEqual(
            (rows[0]["available_delta"], rows[0]["held_delta"]),
            (-VODAFONE_DEBIT, VODAFONE_DEBIT),
        )

    def test_02_usdt_create_success(self):
        """2. USDT create stores native-unit facts and a pinned rate."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request(METHOD_USDT_BEP20)

        row = self.raw_row(req.request_id)
        self.assertEqual(row["native_unit"], "USDT")
        self.assertEqual(row["amount_native_minor"], USDT_AMOUNT_1_5)
        self.assertEqual(row["fee_native_minor"], USDT_FEE_485)
        self.assertEqual(row["amount_egp_minor"], 7275)   # 1.5 * 48.5
        self.assertEqual(row["fee_egp_minor"], 100)
        self.assertEqual(row["rate_usdt_egp"], RATE_TEXT)  # pinned
        self.assertEqual(row["wallet_rate_usdt_egp"], RATE_TEXT)
        self.assertEqual(row["wallet_debit_units"], USDT_DEBIT_1_5)
        # model view round-trips the exact values
        self.assertEqual(req.amount_native, Decimal("1.5"))
        self.assertEqual(req.fee_native, Decimal("0.02061856"))
        self.assertEqual(req.amount_egp, Decimal("72.75"))
        self.assertEqual(req.rate_usdt_egp, Decimal("48.5"))
        self.assertEqual(
            self.wallet_state(501),
            (FUND - USDT_DEBIT_1_5, USDT_DEBIT_1_5),
        )
        self.assertEqual(len(self.ledger_rows(req.request_id)), 1)

    def test_03_exact_wallet_debit_units(self):
        """3. wallet_debit_units is the exact INTEGER USDT amount and
        the single authority shared by row, wallet and ledger."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request()
        self.assertIs(type(req.wallet_debit_units), int)
        with db.get_connection(self.db_path) as conn:
            kind, value = conn.execute(
                "SELECT typeof(wallet_debit_units), wallet_debit_units "
                "FROM withdrawal_requests WHERE request_id = ?",
                (req.request_id,),
            ).fetchone()
        self.assertEqual(kind, "integer")
        self.assertEqual(value, VODAFONE_DEBIT)
        self.assertEqual(self.wallet_state(501)[1], VODAFONE_DEBIT)
        self.assertEqual(
            self.ledger_rows(req.request_id)[0]["amount_units"],
            VODAFONE_DEBIT,
        )

    def test_04_vodafone_ceiling_conversion(self):
        """4. EGP -> USDT converts ONCE with ROUND_CEILING — never a
        rounded-down debit."""
        self.add_user(501)
        self.fund(501, FUND)
        # 11 EGP at 48.5 = 0.22680412371... -> 22_680_413 (ceil)
        req485 = self.create_request()
        self.assertEqual(
            req485.wallet_debit_units, 22_680_413
        )
        self.assertNotEqual(req485.wallet_debit_units, 22_680_412)
        # 11 EGP at 3 = 3.666666666... -> 366_666_667 (ceil)
        self.add_user(502)
        self.fund(502, FUND)
        req3 = self.create_request(
            user_id=502, now=NOW + timedelta(minutes=1),
            quote=_make_quote(Decimal("3")),
        )
        self.assertEqual(
            req3.wallet_debit_units, 366_666_667
        )
        self.assertNotEqual(req3.wallet_debit_units, 366_666_666)

    def test_05_usdt_direct_sum(self):
        """5. USDT wallet debit = amount units + fee units exactly."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request(METHOD_USDT_BEP20)
        self.assertEqual(req.wallet_debit_units, USDT_DEBIT_1_5)
        self.assertEqual(
            req.wallet_debit_units,
            USDT_AMOUNT_1_5 + USDT_FEE_485,
        )

    def test_06_no_usdt_egp_round_trip(self):
        """6. The USDT debit never round-trips through EGP: at rate
        3000 the amount 0.03333335 is 100.00005 EGP (display 100.00);
        converting that display BACK would yield 3_333_334 units and a
        debit of 3_366_668 — the service must keep the direct sum
        3_333_335 + 33_334 = 3_366_669."""
        self.add_user(501)
        self.fund(501, FUND)
        quote = _make_quote(Decimal("3000"))
        req = self.create_request(
            METHOD_USDT_BEP20,
            amount=Decimal("0.03333335"),
            quote=quote,
        )
        row = self.raw_row(req.request_id)
        self.assertEqual(row["amount_native_minor"], 3_333_335)
        self.assertEqual(row["amount_egp_minor"], 10_000)  # 100.00 EGP
        self.assertEqual(
            req.wallet_debit_units,
            3_333_335 + 33_334,
        )
        self.assertNotEqual(req.wallet_debit_units, 3_366_668)
        # structural: the service never touches the EGP->units helper
        # and the one-way display helper is not wired into any debit
        src = inspect.getsource(withdrawal_service)
        self.assertIn("usdt_wallet_debit", src)
        self.assertNotIn("egp_to_wallet_units", src)
        self.assertNotIn("usdt_units_to_egp_display", src)

    def test_07_rate_snapshot_immutable(self):
        """7. The quote's facts persist byte-exactly and never change
        after creation (reject moves only status + timestamp)."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request()
        before = self.raw_row(req.request_id)
        self.assertEqual(before["wallet_rate_usdt_egp"], RATE_TEXT)
        self.assertEqual(before["rate_captured_at"], CAPTURED_TEXT)
        self.assertEqual(before["rate_provider"], "manual")
        self.svc.reject(req.request_id, now=NOW + timedelta(hours=1))
        after = self.raw_row(req.request_id)
        for column in (
            "rate_usdt_egp", "wallet_rate_usdt_egp",
            "rate_captured_at", "rate_provider",
        ):
            self.assertEqual(before[column], after[column])
        self.assertEqual(after["wallet_rate_usdt_egp"], RATE_TEXT)
        self.assertEqual(after["rate_captured_at"], CAPTURED_TEXT)

    def test_08_payment_method_snapshot(self):
        """8. Every pm_* column snapshots the resolved method — the
        platform destination stays in pm_destination."""
        self.add_user(501)
        self.fund(501, FUND)
        method = self.create_method(
            display_name="SNAPSHOT-NAME",
            asset="EGP",
            network="TEST-NET",
            provider="SNAPSHOT-PROV",
            destination=PM_DESTINATION,
        )
        req = self.create_request(payment_method_id=method.id)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["payment_method_id"], method.id)
        self.assertEqual(row["pm_display_name"], "SNAPSHOT-NAME")
        self.assertEqual(row["pm_category"], "cash")
        self.assertEqual(row["pm_asset"], "EGP")
        self.assertEqual(row["pm_network"], "TEST-NET")
        self.assertEqual(row["pm_provider"], "SNAPSHOT-PROV")
        self.assertEqual(row["pm_destination"], PM_DESTINATION)

    def test_09_user_destination_persisted(self):
        """9. user_destination is stored verbatim in ITS column and is
        never the platform destination; None stays None."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request(user_destination=USER_DEST)
        row = self.raw_row(req.request_id)
        self.assertEqual(row["user_destination"], USER_DEST)
        self.assertEqual(row["pm_destination"], PM_DESTINATION)
        self.assertNotEqual(row["user_destination"],
                            row["pm_destination"])
        # omitting the destination stores NULL — never synthesized
        self.add_user(502)
        self.fund(502, FUND)
        req2 = self.create_request(
            user_id=502, now=NOW + timedelta(minutes=1),
            user_destination=None,
        )
        self.assertIsNone(
            self.raw_row(req2.request_id)["user_destination"]
        )

    def test_10_create_failure_rolls_back(self):
        """10. A failure anywhere in the flow rolls back everything:
        no request, no wallet hold, no ledger hold — and the original
        exception propagates unchanged."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = RuntimeError("insert exploded")
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(self.db_path, sentinel),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.create_request(svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])

    def test_11_duplicate_pending_mapped(self):
        """11. A create that reaches the one-pending UNIQUE index
        (cooldown already over) maps to PendingWithdrawalExistsError
        and leaves zero partial state."""
        self.add_user(501)
        self.fund(501, FUND)
        first = self.create_request()
        with self.assertRaises(PendingWithdrawalExistsError):
            self.create_request(now=NOW + timedelta(hours=25))
        # exactly one pending row survived — the failed attempt's
        # wallet reserve and ledger hold were rolled back
        self.assertEqual(self.request_count(501), 1)
        self.assertEqual(self.pending_count(501), 1)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT),
        )
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(
            self.ledger_rows()[0]["reference_id"], first.request_id
        )

    def test_12_cooldown_preserved(self):
        """12. The 24 h cooldown survives: inside the window a new
        request fails (retry_after exposed), rejected requests still
        count, and after 24 h the next request succeeds."""
        self.add_user(501)
        self.fund(501, FUND)
        first = self.create_request()
        with self.assertRaises(CooldownError) as ctx:
            self.create_request(now=NOW + timedelta(hours=1))
        self.assertEqual(ctx.exception.retry_after_seconds, 82_800)
        self.assertEqual(self.request_count(501), 1)
        # a REJECTED request still counts for the cooldown window
        self.svc.reject(first.request_id, now=NOW + timedelta(hours=1))
        with self.assertRaises(CooldownError):
            self.create_request(now=NOW + timedelta(hours=1))
        # after 24 h the next request is allowed (no pending exists)
        second = self.create_request(now=NOW + timedelta(hours=25))
        self.assertEqual(self.request_count(501), 2)
        self.assertEqual(self.pending_count(501), 1)
        # first hold was released on reject; only the second is held
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT),
        )
        self.assertEqual(second.status, RequestStatus.PENDING)

    def test_13_insufficient_wallet_balance(self):
        """13. A wallet that cannot cover the debit raises the DOMAIN
        InsufficientBalanceError and writes nothing."""
        self.add_user(501)
        self.fund(501, 1_000)
        with self.assertRaises(InsufficientBalanceError) as ctx:
            self.create_request()
        self.assertIs(
            type(ctx.exception), InsufficientBalanceError
        )
        self.assertNotIsInstance(ctx.exception,
                                 wallet.WalletError)
        self.assertEqual(self.wallet_state(501), (1_000, 0))
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.ledger_rows(), [])

    def test_14_invalid_amount(self):
        """14. Zero/negative/float/bool/malformed/wrong-precision/
        below-minimum amounts fail as domain errors before any write."""
        self.add_user(501)
        self.fund(501, FUND)
        bad_inputs = (
            Decimal("0"),
            Decimal("-5"),
            10.5,                       # float
            True,                       # bool
            "not-a-number",
            Decimal("10.005"),          # 3 dp for a 2 dp currency
            Decimal("9.99"),            # below the 10 EGP minimum
        )
        for bad in bad_inputs:
            with self.assertRaises(InvalidAmountError, msg=repr(bad)):
                self.create_request(amount=bad)
        # USDT precision + minimum
        for bad in (
            Decimal("0.000000001"),     # 9 dp for an 8 dp currency
            Decimal("0.20618556"),      # one unit below the minimum
        ):
            with self.assertRaises(
                InvalidAmountError, msg=repr(bad)
            ):
                self.create_request(METHOD_USDT_BEP20, amount=bad)
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])

    def test_15_inactive_payment_method(self):
        """15. A deactivated method can never back a new withdrawal."""
        self.add_user(501)
        self.fund(501, FUND)
        inactive = self.create_method(active=False)
        with self.assertRaises(PaymentMethodUnavailableError):
            self.create_request(payment_method_id=inactive.id)
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])

    def test_16_missing_payment_method(self):
        """16. A missing (or malformed) method id fails deterministically."""
        self.add_user(501)
        self.fund(501, FUND)
        with self.assertRaises(PaymentMethodUnavailableError):
            self.create_request(payment_method_id=999_999)
        for bad in (None, 0, -1, True, "abc"):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                self.create_request(payment_method_id=bad)
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(501), (FUND, 0))

    def test_17_no_orphan_ledger_entry(self):
        """17. A create that fails after the ledger hold leaves NO
        ledger row behind."""
        self.add_user(501)
        self.fund(501, FUND)
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(
                self.db_path, RuntimeError("post-hold failure")
            ),
        )
        with self.assertRaises(RuntimeError):
            self.create_request(svc=svc)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.request_count(), 0)

    def test_18_no_orphan_wallet_hold(self):
        """18. A create whose reserve never succeeds holds nothing —
        no wallet hold, no ledger row, no request."""
        self.add_user(501)
        self.fund(501, 1_000)   # far below the debit
        with self.assertRaises(InsufficientBalanceError):
            self.create_request()
        self.assertEqual(self.wallet_state(501), (1_000, 0))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.request_count(), 0)

    # ── create extras ──────────────────────────────────────────

    def test_create_invalid_user_id_rejected(self):
        """Invalid user ids fail before anything is opened."""
        for bad in (0, -1, "1", True, None, 1.5):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                self.create_request(user_id=bad)

    def test_create_unknown_user_rejected(self):
        """An unknown (but well-formed) user is a domain error — no
        wallet, ledger or request state is created."""
        self.add_user(501)
        self.fund(501, FUND)          # user 501 exists but is unused
        with self.assertRaises(ValidationError):
            self.create_request(user_id=42424242)
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.ledger_rows(), [])

    def test_create_requires_explicit_quote(self):
        """No quote -> MissingRateError for BOTH methods; a rate is
        never fetched or invented by the service."""
        self.add_user(501)
        self.fund(501, FUND)
        with self.assertRaises(MissingRateError):
            self.create_request(quote=None)
        with self.assertRaises(MissingRateError):
            self.create_request(METHOD_USDT_BEP20, quote=None)
        self.assertEqual(self.request_count(), 0)

    def test_create_rejects_non_quote_rate(self):
        """Only a validated RateQuote is accepted — a bare Decimal is
        not a quote and cannot smuggle an unvalidated rate in."""
        self.add_user(501)
        self.fund(501, FUND)
        with self.assertRaises(ValidationError):
            self.create_request(quote=Decimal("48.5"))
        self.assertEqual(self.request_count(), 0)

    def test_create_unsupported_method_rejected(self):
        """Methods outside the approved pair fail as InvalidMethodError."""
        self.add_user(501)
        self.fund(501, FUND)
        with self.assertRaises(InvalidMethodError):
            self.create_request(method="paypal")
        self.assertEqual(self.request_count(), 0)

    def test_duplicate_request_id_maps_to_domain(self):
        """Reusing an existing request_id hits the PK: the domain
        DuplicateRequestError surfaces and reserve/hold roll back."""
        self.add_user(501)
        self.fund(501, FUND)
        first = self.create_request()
        self.svc.reject(first.request_id, now=NOW + timedelta(hours=1))
        with self.assertRaises(DuplicateRequestError):
            self.create_request(
                now=NOW + timedelta(hours=25),
                request_id=first.request_id,
            )
        # the second attempt's reserve rolled back; ledger replayed
        # the identical hold idempotently (no new entry)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.request_count(501), 1)
        self.assertEqual(len(self.ledger_rows()), 2)  # hold + release


# ── 19–25: reject ───────────────────────────────────────────────────


class TestReject(_Base):
    def setUp(self):
        super().setUp()
        self.add_user(501)
        self.fund(501, FUND)
        self.req = self.create_request()

    def test_19_pending_to_rejected(self):
        """19. reject moves pending -> rejected and returns the row."""
        out = self.svc.reject(self.req.request_id, now=NOW)
        self.assertEqual(out.status, RequestStatus.REJECTED)
        self.assertEqual(out.request_id, self.req.request_id)
        self.assertEqual(
            self.raw_row(self.req.request_id)["status"], "rejected"
        )

    def test_20_wallet_release_exact(self):
        """20. Exactly wallet_debit_units returns to available."""
        self.svc.reject(self.req.request_id, now=NOW)
        self.assertEqual(self.wallet_state(501), (FUND, 0))

    def test_21_ledger_release_exact(self):
        """21. The release entry mirrors the hold with the canonical
        reference and idempotency key."""
        self.svc.reject(self.req.request_id, now=NOW)
        rows = self.ledger_rows(self.req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in rows], ["hold", "release"]
        )
        release = rows[1]
        self.assertEqual(release["amount_units"], VODAFONE_DEBIT)
        self.assertEqual(
            (release["available_delta"], release["held_delta"]),
            (VODAFONE_DEBIT, -VODAFONE_DEBIT),
        )
        self.assertEqual(
            release["idempotency_key"],
            f"withdrawal:{self.req.request_id}:release",
        )

    def test_22_rejected_at_set(self):
        """22. rejected_at is stamped exactly on the successful
        transition and completed_at stays NULL."""
        at = datetime(2026, 9, 27, 12, 30, 0)
        out = self.svc.reject(self.req.request_id, now=at)
        self.assertEqual(out.rejected_at, at)
        self.assertIsNone(out.completed_at)
        row = self.raw_row(self.req.request_id)
        self.assertEqual(row["rejected_at"], "2026-09-27 12:30:00")
        self.assertIsNone(row["completed_at"])

    def test_23_double_reject_blocked(self):
        """23. A second reject fails and can never release twice."""
        self.svc.reject(self.req.request_id, now=NOW)
        with self.assertRaises(InvalidStateError):
            self.svc.reject(self.req.request_id, now=NOW)
        # money moved exactly once
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        rows = self.ledger_rows(self.req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in rows], ["hold", "release"]
        )

    def test_24_completed_cannot_reject(self):
        """24. A completed request can never be rejected."""
        self.svc.complete(self.req.request_id, now=NOW)
        held_after = self.wallet_state(501)
        with self.assertRaises(InvalidStateError):
            self.svc.reject(self.req.request_id, now=NOW)
        self.assertEqual(self.wallet_state(501), held_after)
        self.assertEqual(
            self.raw_row(self.req.request_id)["status"], "completed"
        )

    def test_25_legacy_null_debit_blocked_safely(self):
        """25. A legacy row with NULL wallet_debit_units can NOT be
        rejected: explicit domain error, no guessing, no mutation."""
        # settle the setUp request first so the one-pending index
        # lets the legacy-shaped row exist for this user
        self.svc.reject(self.req.request_id, now=NOW)
        self.insert_legacy_null_debit("wd-legacy", user_id=501)
        before = self.wallet_state(501)
        with self.assertRaises(MissingWalletDebitError) as ctx:
            self.svc.reject("wd-legacy", now=NOW)
        self.assertIsInstance(ctx.exception, ValidationError)
        self.assertIsInstance(ctx.exception, WithdrawalError)
        row = self.raw_row("wd-legacy")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["rejected_at"])
        self.assertEqual(self.wallet_state(501), before)
        self.assertEqual(self.ledger_rows("wd-legacy"), [])


# ── 26–32: complete ─────────────────────────────────────────────────


class TestComplete(_Base):
    def setUp(self):
        super().setUp()
        self.add_user(501)
        self.fund(501, FUND)
        self.req = self.create_request()

    def test_26_pending_to_completed(self):
        """26. complete moves pending -> completed and returns the row."""
        out = self.svc.complete(self.req.request_id, now=NOW)
        self.assertEqual(out.status, RequestStatus.COMPLETED)
        self.assertEqual(
            self.raw_row(self.req.request_id)["status"], "completed"
        )

    def test_27_wallet_settlement_exact(self):
        """27. Settlement removes the held units only — available is
        NOT deducted a second time."""
        self.svc.complete(self.req.request_id, now=NOW)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, 0),
        )

    def test_28_ledger_settlement_exact(self):
        """28. The settlement entry: held -> gone, available untouched,
        canonical reference and idempotency key."""
        self.svc.complete(self.req.request_id, now=NOW)
        rows = self.ledger_rows(self.req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in rows], ["hold", "settlement"]
        )
        settle = rows[1]
        self.assertEqual(settle["amount_units"], VODAFONE_DEBIT)
        self.assertEqual(
            (settle["available_delta"], settle["held_delta"]),
            (0, -VODAFONE_DEBIT),
        )
        self.assertEqual(
            settle["idempotency_key"],
            f"withdrawal:{self.req.request_id}:settlement",
        )

    def test_29_completed_at_set(self):
        """29. completed_at is stamped exactly on the successful
        transition and rejected_at stays NULL."""
        at = datetime(2026, 9, 27, 13, 45, 0)
        out = self.svc.complete(self.req.request_id, now=at)
        self.assertEqual(out.completed_at, at)
        self.assertIsNone(out.rejected_at)
        row = self.raw_row(self.req.request_id)
        self.assertEqual(row["completed_at"], "2026-09-27 13:45:00")
        self.assertIsNone(row["rejected_at"])

    def test_30_double_complete_blocked(self):
        """30. A second complete fails and can never settle twice."""
        self.svc.complete(self.req.request_id, now=NOW)
        state = self.wallet_state(501)
        with self.assertRaises(InvalidStateError):
            self.svc.complete(self.req.request_id, now=NOW)
        self.assertEqual(self.wallet_state(501), state)
        rows = self.ledger_rows(self.req.request_id)
        self.assertEqual(
            [r["entry_type"] for r in rows], ["hold", "settlement"]
        )

    def test_31_rejected_cannot_complete(self):
        """31. A rejected request can never be completed (its funds
        were already released)."""
        self.svc.reject(self.req.request_id, now=NOW)
        state = self.wallet_state(501)
        with self.assertRaises(InvalidStateError):
            self.svc.complete(self.req.request_id, now=NOW)
        self.assertEqual(self.wallet_state(501), state)
        self.assertEqual(
            self.raw_row(self.req.request_id)["status"], "rejected"
        )

    def test_32_legacy_null_debit_blocked_safely(self):
        """32. A legacy row with NULL wallet_debit_units can NOT be
        settled: explicit domain error and the request stays pending."""
        # settle the setUp request first so the one-pending index
        # lets the legacy-shaped row exist for this user
        self.svc.complete(self.req.request_id, now=NOW)
        self.insert_legacy_null_debit("wd-legacy2", user_id=501)
        before = self.wallet_state(501)
        with self.assertRaises(MissingWalletDebitError):
            self.svc.complete("wd-legacy2", now=NOW)
        row = self.raw_row("wd-legacy2")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["completed_at"])
        self.assertEqual(self.wallet_state(501), before)
        self.assertEqual(self.ledger_rows("wd-legacy2"), [])


# ── 33–40: race / atomicity / connection ownership ─────────────────


class TestRaceAtomicity(_Base):
    def _start(self, target):
        thread = threading.Thread(target=target)
        thread.start()
        return thread

    def test_33_concurrent_duplicate_create_one_pending(self):
        """33. Two concurrent creates for one user yield exactly ONE
        pending request; the loser fails with a deterministic domain
        error and leaves no partial state."""
        self.add_user(501)
        self.fund(501, FUND)
        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                req = self.create_request()
                outcomes.append(("ok", req))
            except WithdrawalError as exc:
                outcomes.append(("err", exc))
            except Exception as exc:  # pragma: no cover
                outcomes.append(("boom", exc))

        threads = [self._start(worker) for _ in range(2)]
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 2)

        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] != "ok"]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(len(errs), 1, outcomes)
        self.assertIsInstance(
            errs[0][1], (CooldownError, PendingWithdrawalExistsError)
        )
        # exactly one pending request, one hold, one debit
        self.assertEqual(self.pending_count(501), 1)
        self.assertEqual(self.request_count(501), 1)
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, VODAFONE_DEBIT),
        )

    def test_34_reject_complete_race_exactly_one_wins(self):
        """34. Concurrent reject vs complete: exactly one succeeds,
        the loser hits InvalidStateError, and money moves exactly once."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request()
        barrier = threading.Barrier(2)
        outcomes = []

        def do_reject():
            barrier.wait()
            try:
                self.svc.reject(req.request_id, now=NOW)
                outcomes.append(("reject", None))
            except Exception as exc:
                outcomes.append(("reject", exc))

        def do_complete():
            barrier.wait()
            try:
                self.svc.complete(req.request_id, now=NOW)
                outcomes.append(("complete", None))
            except Exception as exc:
                outcomes.append(("complete", exc))

        threads = [
            self._start(do_reject),
            self._start(do_complete),
        ]
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 2)

        winners = [label for label, err in outcomes if err is None]
        losers = [(label, err) for label, err in outcomes if err is not None]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIs(type(losers[0][1]), InvalidStateError)

        row = self.raw_row(req.request_id)
        rows = self.ledger_rows(req.request_id)
        if winners[0] == "reject":
            self.assertEqual(row["status"], "rejected")
            self.assertEqual(self.wallet_state(501), (FUND, 0))
            self.assertEqual(
                [r["entry_type"] for r in rows], ["hold", "release"]
            )
        else:
            self.assertEqual(row["status"], "completed")
            self.assertEqual(
                self.wallet_state(501),
                (FUND - VODAFONE_DEBIT, 0),
            )
            self.assertEqual(
                [r["entry_type"] for r in rows],
                ["hold", "settlement"],
            )

    def test_35_every_dependency_receives_the_transaction_connection(self):
        """35. Repository, wallet adapter, ledger adapter and the
        payment-method resolver all run on the exact connection the
        service's transaction yields — and the two flows use two
        distinct transactions."""
        self.add_user(501)
        self.fund(501, FUND)
        seen = {key: [] for key in (
            "tx", "pm", "insert", "transition",
            "reserve", "release", "hold", "ledger_release",
        )}

        real_transaction = db.transaction
        real_resolver = payment_method_store.get_active_payment_method
        real_insert = SqliteWithdrawalRepository.insert
        real_transition = SqliteWithdrawalRepository.transition
        real_reserve = SqliteWalletAdapter.reserve
        real_release = SqliteWalletAdapter.release_units
        real_hold = SqliteLedgerAdapter.hold
        real_ledger_release = SqliteLedgerAdapter.release

        @contextmanager
        def spy_transaction(*args, **kwargs):
            with real_transaction(*args, **kwargs) as conn:
                seen["tx"].append(conn)
                yield conn

        def spy_resolver(method_id, *, connection=None):
            seen["pm"].append(connection)
            return real_resolver(method_id, connection=connection)

        def spy_insert(self_, request, *, connection=None):
            seen["insert"].append(connection)
            return real_insert(self_, request, connection=connection)

        def spy_transition(self_, request_id, *, to_status, at,
                           connection=None):
            seen["transition"].append(connection)
            return real_transition(
                self_, request_id, to_status=to_status, at=at,
                connection=connection,
            )

        def spy_reserve(self_, user_id, amount_units, *, connection=None):
            seen["reserve"].append(connection)
            return real_reserve(
                self_, user_id, amount_units, connection=connection
            )

        def spy_release(self_, user_id, amount_units, *, connection=None):
            seen["release"].append(connection)
            return real_release(
                self_, user_id, amount_units, connection=connection
            )

        def spy_hold(self_, user_id, amount_units, *, request_id,
                     connection, rate_usdt_egp=None):
            seen["hold"].append(connection)
            return real_hold(
                self_, user_id, amount_units, request_id=request_id,
                connection=connection, rate_usdt_egp=rate_usdt_egp,
            )

        def spy_ledger_release(self_, user_id, amount_units, *,
                               request_id, connection,
                               rate_usdt_egp=None):
            seen["ledger_release"].append(connection)
            return real_ledger_release(
                self_, user_id, amount_units, request_id=request_id,
                connection=connection, rate_usdt_egp=rate_usdt_egp,
            )

        with mock.patch.object(db, "transaction", spy_transaction), \
                mock.patch.object(
                    payment_method_store,
                    "get_active_payment_method",
                    spy_resolver,
                ), \
                mock.patch.object(
                    SqliteWithdrawalRepository, "insert", spy_insert
                ), \
                mock.patch.object(
                    SqliteWithdrawalRepository,
                    "transition",
                    spy_transition,
                ), \
                mock.patch.object(
                    SqliteWalletAdapter, "reserve", spy_reserve
                ), \
                mock.patch.object(
                    SqliteWalletAdapter, "release_units", spy_release
                ), \
                mock.patch.object(SqliteLedgerAdapter, "hold", spy_hold), \
                mock.patch.object(
                    SqliteLedgerAdapter, "release", spy_ledger_release
                ):
            svc = WithdrawalService(db_path=self.db_path)
            req = self.create_request(svc=svc)
            svc.reject(req.request_id, now=NOW + timedelta(hours=1))

        self.assertEqual(len(seen["tx"]), 2)
        create_tx, reject_tx = seen["tx"]
        self.assertIsNot(create_tx, reject_tx)
        # create flow — every dependency on the SAME connection
        self.assertIs(seen["pm"][0], create_tx)
        self.assertIs(seen["reserve"][0], create_tx)
        self.assertIs(seen["hold"][0], create_tx)
        self.assertIs(seen["insert"][0], create_tx)
        # reject flow — same story
        self.assertIs(seen["transition"][0], reject_tx)
        self.assertIs(seen["release"][0], reject_tx)
        self.assertIs(seen["ledger_release"][0], reject_tx)

    def test_36_rollback_after_wallet_mutation(self):
        """36. A failure AFTER the wallet reserve rolls the reserve
        back — the wallet returns to its exact pre-create state."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = RuntimeError("hold exploded")
        svc = WithdrawalService(
            db_path=self.db_path,
            ledger_port=_FailingLedger(sentinel),
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.create_request(svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.request_count(), 0)

    def test_37_rollback_after_ledger_mutation(self):
        """37. A failure AFTER the ledger hold rolls back wallet AND
        ledger together."""
        self.add_user(501)
        self.fund(501, FUND)
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(
                self.db_path, RuntimeError("insert failed")
            ),
        )
        with self.assertRaises(RuntimeError):
            self.create_request(svc=svc)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.request_count(), 0)

    def test_38_unexpected_exception_propagates(self):
        """38. Unexpected exceptions escape as the SAME object (never
        swallowed/re-labeled) and the transaction still rolls back."""
        self.add_user(501)
        self.fund(501, FUND)
        sentinel = ValueError("totally unexpected")
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(self.db_path, sentinel),
        )
        with self.assertRaises(ValueError) as ctx:
            self.create_request(svc=svc)
        self.assertIs(ctx.exception, sentinel)
        self.assertNotIsInstance(ctx.exception, WithdrawalError)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.request_count(), 0)

    def test_39_idempotency_keys_deterministic(self):
        """39. Ledger keys are exactly withdrawal:<id>:<entry_type>
        on every path."""
        self.add_user(501)
        self.fund(501, FUND)
        first = self.create_request()
        self.svc.reject(first.request_id, now=NOW)
        self.add_user(502)
        self.fund(502, FUND)
        second = self.create_request(
            user_id=502, now=NOW + timedelta(minutes=1)
        )
        self.svc.complete(second.request_id, now=NOW)

        first_keys = [
            r["idempotency_key"]
            for r in self.ledger_rows(first.request_id)
        ]
        second_keys = [
            r["idempotency_key"]
            for r in self.ledger_rows(second.request_id)
        ]
        self.assertEqual(
            first_keys,
            [
                f"withdrawal:{first.request_id}:hold",
                f"withdrawal:{first.request_id}:release",
            ],
        )
        self.assertEqual(
            second_keys,
            [
                f"withdrawal:{second.request_id}:hold",
                f"withdrawal:{second.request_id}:settlement",
            ],
        )
        # every row carries the withdrawal reference
        for row in self.ledger_rows():
            self.assertEqual(row["reference_type"], "withdrawal")

    def test_40_no_double_money_movement(self):
        """40. Full lifecycles move money exactly once — no second
        deduction on complete, no second release on reject, and every
        repeat attempt fails without touching balances."""
        self.add_user(501)
        self.add_user(502)
        self.fund(501, FUND)
        self.fund(502, FUND)

        # complete path: net -debit, held drains to zero
        done = self.create_request(user_id=501)
        self.svc.complete(done.request_id, now=NOW)
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, 0),
        )
        for repeat in (
            lambda: self.svc.complete(done.request_id, now=NOW),
            lambda: self.svc.reject(done.request_id, now=NOW),
        ):
            with self.assertRaises(InvalidStateError):
                repeat()
        self.assertEqual(
            self.wallet_state(501),
            (FUND - VODAFONE_DEBIT, 0),
        )
        self.assertEqual(
            [r["entry_type"] for r in self.ledger_rows(done.request_id)],
            ["hold", "settlement"],
        )

        # reject path: net zero, everything returned exactly once
        back = self.create_request(user_id=502)
        self.svc.reject(back.request_id, now=NOW)
        self.assertEqual(self.wallet_state(502), (FUND, 0))
        with self.assertRaises(InvalidStateError):
            self.svc.reject(back.request_id, now=NOW)
        self.assertEqual(self.wallet_state(502), (FUND, 0))
        self.assertEqual(
            [r["entry_type"] for r in self.ledger_rows(back.request_id)],
            ["hold", "release"],
        )

    def test_no_hidden_connection_while_flow_active(self):
        """Part M: no code path opens a hidden connection while the
        service's transaction is active — create, reject and complete
        all run with db.get_connection() completely untouched."""
        self.add_user(501)
        self.fund(501, FUND)
        calls = []

        def boom(*_args, **_kwargs):
            calls.append(1)
            raise AssertionError("hidden connection opened")

        with mock.patch.object(db, "get_connection", side_effect=boom):
            first = self.create_request()
            self.svc.reject(
                first.request_id, now=NOW + timedelta(hours=1)
            )
            second = self.create_request(
                now=NOW + timedelta(hours=25)
            )
            self.svc.complete(
                second.request_id, now=NOW + timedelta(hours=26)
            )
        self.assertEqual(calls, [])
        self.assertEqual(self.pending_count(501), 0)
        self.assertEqual(self.request_count(501), 2)

    def test_rollback_restores_pre_operation_state(self):
        """M: after a failed flow the database equals its exact
        pre-operation state (request, wallet, ledger)."""
        self.add_user(501)
        self.fund(501, FUND)
        baseline_wallet = self.wallet_state(501)
        svc = WithdrawalService(
            db_path=self.db_path,
            repository=_FailingRepository(
                self.db_path, RuntimeError("boom")
            ),
        )
        with self.assertRaises(RuntimeError):
            self.create_request(svc=svc)
        self.assertEqual(self.wallet_state(501), baseline_wallet)
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.request_count(), 0)


# ── error-boundary extras (part L) ──────────────────────────────────


class TestErrorBoundary(_Base):
    def test_unknown_request_is_request_not_found(self):
        """Missing ids are deterministic RequestNotFoundError in both
        flows."""
        self.add_user(501)
        with self.assertRaises(RequestNotFoundError):
            self.svc.reject("wd-does-not-exist", now=NOW)
        with self.assertRaises(RequestNotFoundError):
            self.svc.complete("wd-does-not-exist", now=NOW)

    def test_insufficient_held_translates_and_rolls_back(self):
        """wallet.InsufficientHeldBalanceError becomes the DOMAIN
        error at the service edge, and the failed CAS is rolled back:
        the request stays pending with no timestamp."""
        self.add_user(501)
        self.fund(501, FUND)            # available, held = 0
        self.insert_legacy_big_debit("wd-big", user_id=501,
                                     debit=999_999_999)
        for op in (
            lambda: self.svc.reject("wd-big", now=NOW),
            lambda: self.svc.complete("wd-big", now=NOW),
        ):
            with self.assertRaises(InsufficientHeldBalanceError) as ctx:
                op()
            self.assertIs(type(ctx.exception),
                          InsufficientHeldBalanceError)
            self.assertNotIsInstance(ctx.exception, wallet.WalletError)
            row = self.raw_row("wd-big")
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["rejected_at"])
            self.assertIsNone(row["completed_at"])
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows("wd-big"), [])


# ── 41–44: financial safety ─────────────────────────────────────────


class TestFinancialSafety(_Base):
    def test_41_sub_cent_usdt_stays_exact(self):
        """41. A sub-cent USDT withdrawal (rate 3000: the exact 10 EGP
        minimum is 0.00333334 USDT = 333_334 units < 0.01 USDT) stores
        exact integer atomic units — amount and fee are both below one
        cent and nothing rounds."""
        self.add_user(501)
        self.fund(501, FUND)
        quote = _make_quote(Decimal("3000"))
        req = self.create_request(
            METHOD_USDT_BEP20, amount=SUBCENT_AMOUNT, quote=quote
        )
        row = self.raw_row(req.request_id)
        self.assertEqual(row["amount_native_minor"],
                         SUBCENT_AMOUNT_UNITS)
        self.assertEqual(row["fee_native_minor"], SUBCENT_FEE_UNITS)
        self.assertLess(row["amount_native_minor"], 1_000_000)
        self.assertLess(row["fee_native_minor"], 1_000_000)
        self.assertEqual(row["amount_egp_minor"], 1000)  # 10.00 EGP
        self.assertEqual(row["fee_egp_minor"], 100)
        self.assertEqual(row["wallet_debit_units"], SUBCENT_DEBIT)
        self.assertEqual(
            req.wallet_debit_units,
            SUBCENT_AMOUNT_UNITS + SUBCENT_FEE_UNITS,
        )
        self.assertEqual(self.wallet_state(501),
                         (FUND - SUBCENT_DEBIT, SUBCENT_DEBIT))
        # read-back is exact
        self.assertEqual(
            self.svc._repository.get(req.request_id).wallet_debit_units,
            SUBCENT_DEBIT,
        )

    def test_42_integer_only_accounting(self):
        """42. Every money column lives in the INTEGER (or TEXT rate)
        domain; float input is rejected before any write."""
        self.add_user(501)
        self.fund(501, FUND)
        req = self.create_request()
        with db.get_connection(self.db_path) as conn:
            kinds = conn.execute(
                "SELECT typeof(amount_egp_minor), typeof(fee_egp_minor),"
                "       typeof(amount_native_minor),"
                "       typeof(fee_native_minor),"
                "       typeof(wallet_debit_units),"
                "       typeof(wallet_rate_usdt_egp)"
                "  FROM withdrawal_requests WHERE request_id = ?",
                (req.request_id,),
            ).fetchone()
        self.assertEqual(
            list(kinds),
            ["integer", "integer", "integer", "integer",
             "integer", "text"],
        )
        self.assertIs(type(req.wallet_debit_units), int)
        # a float never reaches the service
        before = self.request_count()
        with self.assertRaises(InvalidAmountError):
            self.create_request(amount=10.5)
        self.assertEqual(self.request_count(), before)

    def test_43_no_float_and_no_real(self):
        """43. The service source contains no float literal/call/
        reference (isinstance guards aside), no REAL token, and the
        schema declares no floating-point column."""
        src = inspect.getsource(withdrawal_service)
        tree = ast.parse(src)
        guarded: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                for arg in node.args[1:]:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Name):
                            guarded.add(sub.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) \
                    and isinstance(node.value, float):
                self.fail(f"float literal at line {node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                self.fail(f"float() call at line {node.lineno}")
            if (
                isinstance(node, ast.Name)
                and node.id == "float"
                and node.id not in guarded
            ):
                self.fail(f"float reference at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "float":
                self.fail(f"float attribute at line {node.lineno}")
        self.assertNotIn("REAL", src.upper())
        declared = [c[2].upper() for c in self.column_types()]
        for banned in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
            self.assertNotIn(banned, declared)

    def test_44_int64_boundary(self):
        """44. Amounts whose atomic totals exceed the SQLite INTEGER
        range fail as domain errors BEFORE any wallet/ledger write."""
        self.add_user(501)
        self.fund(501, FUND)
        # EGP path: 99_999_999_999_999_999.99 EGP * 100 minor overflows
        with self.assertRaises(InvalidAmountError):
            self.create_request(
                amount=Decimal("99999999999999999.99")
            )
        # USDT path: amount alone is exactly INT64_MAX units; adding
        # the fee must overflow instead of wrapping
        with self.assertRaises(InvalidAmountError):
            self.create_request(
                METHOD_USDT_BEP20,
                amount=Decimal("92233720368.54775807"),
            )
        self.assertEqual(self.request_count(), 0)
        self.assertEqual(self.wallet_state(501), (FUND, 0))
        self.assertEqual(self.ledger_rows(), [])


if __name__ == "__main__":
    unittest.main()
